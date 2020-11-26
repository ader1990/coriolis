# Copyright 2020 Cloudbase Solutions Srl
# All Rights Reserved.

import datetime
import math
import uuid

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import timeutils
from taskflow import deciders as taskflow_deciders
from taskflow.patterns import graph_flow
from taskflow.patterns import linear_flow
from taskflow.patterns import unordered_flow

from coriolis import constants
from coriolis import context
from coriolis import exception
from coriolis import utils
from coriolis.conductor.rpc import client as rpc_conductor_client
from coriolis.cron import cron
from coriolis.db import api as db_api
from coriolis.db.sqlalchemy import models
from coriolis.minion_manager.rpc import client as rpc_minion_manager_client
from coriolis.minion_manager.rpc import tasks as minion_manager_tasks
from coriolis.minion_manager.rpc import utils as minion_manager_utils
from coriolis.scheduler.rpc import client as rpc_scheduler_client
from coriolis.taskflow import runner as taskflow_runner
from coriolis.worker.rpc import client as rpc_worker_client


VERSION = "1.0"

LOG = logging.getLogger(__name__)

MINION_MANAGER_OPTS = [
    cfg.IntOpt(
        "minion_pool_default_refresh_period_minutes",
        default=10,
        help="Number of minutes in which to refresh minion pools.")]

CONF = cfg.CONF
CONF.register_opts(MINION_MANAGER_OPTS, 'minion_manager')

MINION_POOL_REFRESH_CRON_JOB_NAME_FORMAT = "pool-%s-refresh-minute-%d"
MINION_POOL_REFRESH_CRON_JOB_DESCRIPTION_FORMAT = (
    "Regularly scheduled refresh job for minion pool '%s' on minute %d.")


def _trigger_pool_refresh(ctxt, minion_manager_client, minion_pool_id):
    try:
        minion_manager_client.refresh_minion_pool(
            ctxt, minion_pool_id)
    except exception.InvalidMinionPoolState as ex:
        LOG.warn(
            "Minion Pool '%s' is in an invalid state for having a refresh run."
            " Skipping for now. Error was: %s", minion_pool_id, str(ex))


class MinionManagerServerEndpoint(object):

    def __init__(self):
        self._cron = cron.Cron()
        self._admin_ctxt = context.get_admin_context()
        # self._init_cron()

    def _init_cron(self):
        now = timeutils.utcnow()
        minion_pools = db_api.get_minion_pools(
            self._admin_ctxt, include_machines=False,
            include_progress_updates=False, include_events=False)
        for minion_pool in minion_pools:
            active_pool_statuses = [constants.MINION_POOL_STATUS_ALLOCATED]
            if minion_pool.status not in active_pool_statuses:
                LOG.debug(
                    "Not setting any refresh schedules for minion pool '%s' "
                    "as it is in an inactive status '%s'.",
                    minion_pool.id, minion_pool.status)
                continue
            LOG.debug(
                "Adding refresh schedule for minion pool '%s' as part of "
                "server startup.", minion_pool.id)
            self._register_refresh_jobs_for_minion_pool(minion_pool, date=now)

    def _register_refresh_jobs_for_minion_pool(
            self, minion_pool, date=None, period_minutes=None):
        if not period_minutes:
            period_minutes = CONF.minion_manager.minion_pool_default_refresh_period_minutes
        if period_minutes <= 0:
            LOG.warn(
                "Got zero or negative pool refresh period %s. Defaulting to "
                "1.", period_minutes)
            period_minutes = 1
        if period_minutes > 60:
            LOG.warn(
                "Selected pool refresh period_minutes is greater than 60, defaulting "
                "to 10. Original value was: %s", period_minutes)
            period_minutes = 10
        if not date:
            date = timeutils.utcnow()
        admin_ctxt = context.get_admin_context()
        description = (
            "Scheduled refresh job for minion pool '%s'" % minion_pool.id)

        # NOTE: we need to generate hourly schedules for each minute in
        # the hour we would like the refresh to be triggered:
        for minute in [
                period_minutes * i for i in range(
                    math.ceil(60 / period_minutes))]:
            name = MINION_POOL_REFRESH_CRON_JOB_NAME_FORMAT % (
                minion_pool.id, minute)
            description = MINION_POOL_REFRESH_CRON_JOB_DESCRIPTION_FORMAT % (
                minion_pool.id, minute)
            self._cron.register(
                cron.CronJob(
                    name, description, {"minute": minute}, True, None, None,
                    None, _trigger_pool_refresh, admin_ctxt,
                    self._rpc_minion_manager_client, minion_pool.id))

    @property
    def _taskflow_runner(self):
        return taskflow_runner.TaskFlowRunner(
            constants.MINION_MANAGER_MAIN_MESSAGING_TOPIC,
            max_workers=25)

    # NOTE(aznashwan): it is unsafe to fork processes with pre-instantiated
    # oslo_messaging clients as the underlying eventlet thread queues will
    # be invalidated. Considering this class both serves from a "main
    # process" as well as forking child processes, it is safest to
    # re-instantiate the clients every time:
    @property
    def _rpc_worker_client(self):
        return rpc_worker_client.WorkerClient()

    @property
    def _rpc_scheduler_client(self):
        return rpc_scheduler_client.SchedulerClient()

    @property
    def _rpc_conductor_client(self):
        return rpc_conductor_client.ConductorClient()

    @property
    def _rpc_minion_manager_client(self):
        return rpc_minion_manager_client.MinionManagerClient()

    def get_diagnostics(self, ctxt):
        return utils.get_diagnostics_info()

    def get_endpoint_source_minion_pool_options(
            self, ctxt, endpoint_id, env, option_names):
        endpoint = self._rpc_conductor_client.get_endpoint(ctxt, endpoint_id)

        worker_service = self._rpc_scheduler_client.get_worker_service_for_specs(
            ctxt, enabled=True,
            region_sets=[[reg['id'] for reg in endpoint['mapped_regions']]],
            provider_requirements={
                endpoint['type']: [
                    constants.PROVIDER_TYPE_SOURCE_MINION_POOL]})
        worker_rpc = rpc_worker_client.WorkerClient.from_service_definition(
            worker_service)

        return worker_rpc.get_endpoint_source_minion_pool_options(
            ctxt, endpoint['type'], endpoint['connection_info'], env,
            option_names)

    def get_endpoint_destination_minion_pool_options(
            self, ctxt, endpoint_id, env, option_names):
        endpoint = self._rpc_conductor_client.get_endpoint(ctxt, endpoint_id)

        worker_service = self._rpc_scheduler_client.get_worker_service_for_specs(
            ctxt, enabled=True,
            region_sets=[[reg['id'] for reg in endpoint['mapped_regions']]],
            provider_requirements={
                endpoint['type']: [
                    constants.PROVIDER_TYPE_DESTINATION_MINION_POOL]})
        worker_rpc = rpc_worker_client.WorkerClient.from_service_definition(
            worker_service)

        return worker_rpc.get_endpoint_destination_minion_pool_options(
            ctxt, endpoint['type'], endpoint['connection_info'], env,
            option_names)

    def validate_endpoint_source_minion_pool_options(
            self, ctxt, endpoint_id, pool_environment):
        endpoint = self._rpc_conductor_client.get_endpoint(ctxt, endpoint_id)

        worker_service = self._rpc_scheduler_client.get_worker_service_for_specs(
            ctxt, enabled=True,
            region_sets=[[reg['id'] for reg in endpoint['mapped_regions']]],
            provider_requirements={
                endpoint['type']: [
                    constants.PROVIDER_TYPE_SOURCE_MINION_POOL]})
        worker_rpc = rpc_worker_client.WorkerClient.from_service_definition(
            worker_service)

        return worker_rpc.validate_endpoint_source_minion_pool_options(
            ctxt, endpoint['type'], pool_environment)

    def validate_endpoint_destination_minion_pool_options(
            self, ctxt, endpoint_id, pool_environment):
        endpoint = self._rpc_conductor_client.get_endpoint(ctxt, endpoint_id)

        worker_service = self._rpc_scheduler_client.get_worker_service_for_specs(
            ctxt, enabled=True,
            region_sets=[[reg['id'] for reg in endpoint['mapped_regions']]],
            provider_requirements={
                endpoint['type']: [
                    constants.PROVIDER_TYPE_DESTINATION_MINION_POOL]})
        worker_rpc = rpc_worker_client.WorkerClient.from_service_definition(
            worker_service)

        return worker_rpc.validate_endpoint_destination_minion_pool_options(
            ctxt, endpoint['type'], pool_environment)

    @minion_manager_utils.minion_pool_synchronized_op
    def add_minion_pool_event(self, ctxt, minion_pool_id, level, message):
        LOG.info(
            "Minion pool event for pool %s: %s", minion_pool_id, message)
        pool = db_api.get_minion_pool(ctxt, minion_pool_id)
        db_api.add_minion_pool_event(ctxt, pool.id, level, message)

    @minion_manager_utils.minion_pool_synchronized_op
    def add_minion_pool_progress_update(
            self, ctxt, minion_pool_id, total_steps, message):
        LOG.info(
            "Adding pool progress update for %s: %s", minion_pool_id, message)
        db_api.add_minion_pool_progress_update(
            ctxt, minion_pool_id, total_steps, message)

    @minion_manager_utils.minion_pool_synchronized_op
    def update_minion_pool_progress_update(
            self, ctxt, minion_pool_id, step, total_steps, message):
        LOG.info("Updating minion pool progress update: %s", minion_pool_id)
        db_api.update_minion_pool_progress_update(
            ctxt, minion_pool_id, step, total_steps, message)

    @minion_manager_utils.minion_pool_synchronized_op
    def get_minion_pool_progress_step(self, ctxt, minion_pool_id):
        return db_api.get_minion_pool_progress_step(ctxt, minion_pool_id)

    def _check_keys_for_action_dict(
            self, action, required_action_properties, operation=None):
        if not isinstance(action, dict):
            raise exception.InvalidInput(
                "Action must be a dict, got '%s': %s" % (
                    type(action), action))
        missing = [
            prop for prop in required_action_properties
            if prop not in action]
        if missing:
            raise exception.InvalidInput(
                "Missing the following required action properties for "
                "%s: %s. Got %s" % (
                    operation, missing, action))

    def validate_minion_pool_selections_for_action(self, ctxt, action):
        """ Validates the minion pool selections for a given action. """
        required_action_properties = [
            'id', 'origin_endpoint_id', 'destination_endpoint_id',
            'origin_minion_pool_id', 'destination_minion_pool_id',
            'instance_osmorphing_minion_pool_mappings', 'instances']
        self._check_keys_for_action_dict(
            action, required_action_properties,
            operation="minion pool selection validation")

        minion_pools = {
            pool.id: pool
            # NOTE: we can just load all the pools in one go to
            # avoid extraneous DB queries:
            for pool in db_api.get_minion_pools(
                ctxt, include_machines=False, include_events=False,
                include_progress_updates=False, to_dict=False)}
        def _get_pool(pool_id):
            pool = minion_pools.get(pool_id)
            if not pool:
                raise exception.NotFound(
                    "Could not find minion pool with ID '%s'." % pool_id)
            return pool
        def _check_pool_minion_count(
                minion_pool, instances, minion_pool_type=""):
            desired_minion_count = len(instances)
            if minion_pool.status != constants.MINION_POOL_STATUS_ALLOCATED:
                raise exception.InvalidMinionPoolState(
                    "Minion Pool '%s' is an invalid state ('%s') to be "
                    "used as a %s pool for action '%s'. The pool must be "
                    "in '%s' status."  % (
                        minion_pool.id, minion_pool.status,
                        minion_pool_type.lower(), action['id'],
                        constants.MINION_POOL_STATUS_ALLOCATED))
            if desired_minion_count > minion_pool.maximum_minions:
                msg = (
                    "Minion Pool with ID '%s' has a lower maximum minion "
                    "count (%d) than the requested number of minions "
                    "(%d) to handle all of the instances of action '%s': "
                    "%s" % (
                        minion_pool.id, minion_pool.maximum_minions,
                        desired_minion_count, action['id'], instances))
                if minion_pool_type:
                    msg = "%s %s" % (minion_pool_type, msg)
                raise exception.InvalidMinionPoolSelection(msg)

        # check source pool:
        instances = action['instances']
        if action['origin_minion_pool_id']:
            origin_pool = _get_pool(action['origin_minion_pool_id'])
            if origin_pool.endpoint_id != action['origin_endpoint_id']:
                raise exception.InvalidMinionPoolSelection(
                    "The selected origin minion pool ('%s') belongs to a "
                    "different Coriolis endpoint ('%s') than the requested "
                    "origin endpoint ('%s')" % (
                        action['origin_minion_pool_id'],
                        origin_pool.endpoint_id,
                        action['origin_endpoint_id']))
            if origin_pool.platform != constants.PROVIDER_PLATFORM_SOURCE:
                raise exception.InvalidMinionPoolSelection(
                    "The selected origin minion pool ('%s') is configured as a"
                    " '%s' pool. The pool must be of type %s to be used for "
                    "data exports." % (
                        action['origin_minion_pool_id'],
                        origin_pool.platform,
                        constants.PROVIDER_PLATFORM_SOURCE))
            if origin_pool.os_type != constants.OS_TYPE_LINUX:
                raise exception.InvalidMinionPoolSelection(
                    "The selected origin minion pool ('%s') is of OS type '%s'"
                    " instead of the Linux OS type required for a source "
                    "transfer minion pool." % (
                        action['origin_minion_pool_id'],
                        origin_pool.os_type))
            _check_pool_minion_count(
                origin_pool, instances, minion_pool_type="Source")
            LOG.debug(
                "Successfully validated compatibility of origin minion pool "
                "'%s' for use with action '%s'." % (
                    action['origin_minion_pool_id'], action['id']))

        # check destination pool:
        if action['destination_minion_pool_id']:
            destination_pool = _get_pool(action['destination_minion_pool_id'])
            if destination_pool.endpoint_id != (
                    action['destination_endpoint_id']):
                raise exception.InvalidMinionPoolSelection(
                    "The selected destination minion pool ('%s') belongs to a "
                    "different Coriolis endpoint ('%s') than the requested "
                    "destination endpoint ('%s')" % (
                        action['destination_minion_pool_id'],
                        destination_pool.endpoint_id,
                        action['destination_endpoint_id']))
            if destination_pool.platform != (
                    constants.PROVIDER_PLATFORM_DESTINATION):
                raise exception.InvalidMinionPoolSelection(
                    "The selected destination minion pool ('%s') is configured"
                    " as a '%s'. The pool must be of type %s to be used for "
                    "data imports." % (
                        action['destination_minion_pool_id'],
                        destination_pool.platform,
                        constants.PROVIDER_PLATFORM_DESTINATION))
            if destination_pool.os_type != constants.OS_TYPE_LINUX:
                raise exception.InvalidMinionPoolSelection(
                    "The selected destination minion pool ('%s') is of OS type"
                    " '%s' instead of the Linux OS type required for a source "
                    "transfer minion pool." % (
                        action['destination_minion_pool_id'],
                        destination_pool.os_type))
            _check_pool_minion_count(
                destination_pool, instances,
                minion_pool_type="Destination")
            LOG.debug(
                "Successfully validated compatibility of destination minion "
                "pool '%s' for use with action '%s'." % (
                    action['origin_minion_pool_id'], action['id']))

        # check OSMorphing pool(s):
        instance_osmorphing_minion_pool_mappings = action.get(
            'instance_osmorphing_minion_pool_mappings')
        if instance_osmorphing_minion_pool_mappings:
            osmorphing_pool_mappings = {}
            for (instance_id, pool_id) in (
                    instance_osmorphing_minion_pool_mappings).items():
                if instance_id not in instances:
                    LOG.warn(
                        "Ignoring OSMorphing pool validation for instance with"
                        " ID '%s' (mapped pool '%s') as it is not part of  "
                        "action '%s's declared instances: %s",
                        instance_id, pool_id, action['id'], instances)
                    continue
                if pool_id not in osmorphing_pool_mappings:
                    osmorphing_pool_mappings[pool_id] = [instance_id]
                else:
                    osmorphing_pool_mappings[pool_id].append(instance_id)

            for (pool_id, instances_to_osmorph) in osmorphing_pool_mappings.items():
                osmorphing_pool = _get_pool(pool_id)
                if osmorphing_pool.endpoint_id != (
                        action['destination_endpoint_id']):
                    raise exception.InvalidMinionPoolSelection(
                        "The selected OSMorphing minion pool for instances %s"
                        " ('%s') belongs to a different Coriolis endpoint "
                        "('%s') than the destination endpoint ('%s')" % (
                            instances_to_osmorph, pool_id,
                            osmorphing_pool.endpoint_id,
                            action['destination_endpoint_id']))
                if osmorphing_pool.platform != (
                        constants.PROVIDER_PLATFORM_DESTINATION):
                    raise exception.InvalidMinionPoolSelection(
                        "The selected OSMorphing minion pool for instances %s "
                        "('%s') is configured as a '%s' pool. The pool must "
                        "be of type %s to be used for OSMorphing." % (
                            instances_to_osmorph, pool_id,
                            osmorphing_pool.platform,
                            constants.PROVIDER_PLATFORM_DESTINATION))
                _check_pool_minion_count(
                    osmorphing_pool, instances_to_osmorph,
                    minion_pool_type="OSMorphing")
                LOG.debug(
                    "Successfully validated compatibility of destination "
                    "minion pool '%s' for use as OSMorphing minion for "
                    "instances %s during action '%s'." % (
                        pool_id, instances_to_osmorph, action['id']))
        LOG.debug(
            "Successfully validated minion pool selections for action '%s' "
            "with properties: %s", action['id'], action)

    def allocate_minion_machines_for_replica(
            self, ctxt, replica):
        try:
            minion_allocations = self._run_machine_allocation_subflow_for_action(
                ctxt, replica, constants.TRANSFER_ACTION_TYPE_REPLICA,
                include_transfer_minions=True,
                include_osmorphing_minions=False)
        except Exception as ex:
            LOG.warn(
                "Error occured while reporting minion pool allocations for "
                "Replica with ID '%s'. Removing all allocations. "
                "Error was: %s" % (
                    replica['id'], utils.get_exception_details()))
            self._cleanup_machines_with_statuses_for_action(
                ctxt, replica['id'],
                [constants.MINION_MACHINE_STATUS_UNINITIALIZED])
            self.deallocate_minion_machines_for_action(
                ctxt, replica['id'])
            self._rpc_conductor_client.report_replica_minions_allocation_error(
                ctxt, replica['id'], str(ex))
            raise

    def allocate_minion_machines_for_migration(
            self, ctxt, migration, include_transfer_minions=True,
            include_osmorphing_minions=True):
        try:
            self._run_machine_allocation_subflow_for_action(
                ctxt, migration,
                constants.TRANSFER_ACTION_TYPE_MIGRATION,
                include_transfer_minions=include_transfer_minions,
                include_osmorphing_minions=include_osmorphing_minions)
        except Exception as ex:
            LOG.warn(
                "Error occured while reporting minion pool allocations for "
                "Migration with ID '%s'. Removing all allocations. "
                "Error was: %s" % (
                    migration['id'], utils.get_exception_details()))
            self._cleanup_machines_with_statuses_for_action(
                ctxt, migration['id'],
                [constants.MINION_MACHINE_STATUS_UNINITIALIZED])
            self.deallocate_minion_machines_for_action(
                ctxt, migration['id'])
            self._rpc_conductor_client.report_migration_minions_allocation_error(
                ctxt, migration['id'], str(ex))
            raise

    def _make_minion_machine_allocation_subflow_for_action(
            self, ctxt, minion_pool, action_id, action_instances,
            subflow_name, inject_for_tasks=None):
        """ Creates a subflow for allocating minion machines from the
        provided minion pool to the given action (one for each instance)

        Returns a mapping between the action's instaces' IDs and the minion
        machine ID, as well as the subflow to execute for said machines.

        Returns dict of the form: {
            "flow": TheFlowClass(),
            "action_instance_minion_allocation_mappings": {
                "<action_instance_id>": "<allocated_minion_id>"}}
        """
        currently_available_machines = [
            machine for machine in minion_pool.minion_machines
            if machine.status == constants.MINION_MACHINE_STATUS_AVAILABLE]
        extra_available_machine_slots = (
            minion_pool.maximum_minions - len(minion_pool.minion_machines))
        num_instances = len(action_instances)
        num_currently_available_machines = len(currently_available_machines)
        if num_instances > (len(currently_available_machines) + (
                                extra_available_machine_slots)):
            raise exception.InvalidMinionPoolState(
                "Minion pool '%s' is unable to accommodate the requested "
                "number of machines (%s) for transfer action '%s', as it only "
                "has %d currently available machines, with room to upscale a "
                "further %d until the maximum is reached. Please either "
                "increase the number of maximum machines for the pool "
                "or wait for other minions to become available before "
                "retrying." % (
                    minion_pool.id, num_instances, action_id,
                    num_currently_available_machines,
                    extra_available_machine_slots))

        def _select_machine(minion_pool, exclude=None):
            selected_machine = None
            for machine in minion_pool.minion_machines:
                if exclude and machine.id in exclude:
                    LOG.debug(
                        "Excluding minion machine '%s' from search for use "
                        "action '%s'", machine.id, action_id)
                    continue
                if machine.status != constants.MINION_MACHINE_STATUS_AVAILABLE:
                    LOG.debug(
                        "Minion machine with ID '%s' is in status '%s' "
                        "instead of the expected '%s'. Skipping for use "
                        "with action '%s'.",
                        machine.id, machine.status,
                        constants.MINION_MACHINE_STATUS_AVAILABLE, action_id)
                    continue
                selected_machine = machine
                break
            return selected_machine

        allocation_subflow = unordered_flow.Flow(subflow_name)
        instance_minion_allocations = {}
        machine_db_entries_to_add = []
        existing_machines_to_allocate = {}
        for instance in action_instances:

            if instance in instance_minion_allocations:
                raise exception.InvalidInput(
                    "Instance with identifier '%s' passed twice for "
                    "minion machine allocation from pool '%s' for action "
                    "'%s'. Full instances list was: %s" % (
                        instance, minion_pool.id, action_id, action_instances))
            minion_machine = _select_machine(
                minion_pool, exclude=instance_minion_allocations.values())
            if minion_machine:
                # take note of the machine and setup a healthcheck:
                instance_minion_allocations[instance] = minion_machine.id
                existing_machines_to_allocate[minion_machine.id] = instance
                LOG.debug(
                    "Allocating pre-existing machine '%s' from pool '%s' for "
                    "use with action with ID '%s'.",
                    minion_machine.id, minion_pool.id, action_id)
                allocation_subflow.add(
                    self._get_healtchcheck_flow_for_minion_machine(
                        minion_pool, minion_machine.id,
                        allocate_to_action=action_id,
                        inject_for_tasks=inject_for_tasks,
                        machine_status_on_success=(
                            constants.MINION_MACHINE_STATUS_IN_USE)))
            else:
                # add task which creates the new machine:
                new_machine_id = str(uuid.uuid4())
                LOG.debug(
                    "New minion machine with ID '%s' will be created for "
                    "minion pool '%s' for use with action '%s'.",
                    new_machine_id, minion_pool.id, action_id)

                new_minion_machine = models.MinionMachine()
                new_minion_machine.id = new_machine_id
                new_minion_machine.pool_id = minion_pool.id
                new_minion_machine.status = (
                    constants.MINION_MACHINE_STATUS_UNINITIALIZED)
                new_minion_machine.allocated_action = action_id
                machine_db_entries_to_add.append(new_minion_machine)

                instance_minion_allocations[instance] = new_machine_id
                allocation_subflow.add(
                    minion_manager_tasks.AllocateMinionMachineTask(
                        minion_pool.id, new_machine_id, minion_pool.platform,
                        allocate_to_action=action_id,
                        raise_on_cleanup_failure=False,
                        inject=inject_for_tasks))

        new_machine_db_entries_added = []
        try:
            # mark any existing machines as allocated:
            LOG.debug(
                "Marking the following pre-existing minion machines "
                "from pool '%s' of action '%s' for each instance as "
                "allocated with the DB: %s",
                minion_pool.id, action_id, existing_machines_to_allocate)
            db_api.set_minion_machines_allocation_statuses(
                ctxt, list(existing_machines_to_allocate.keys()),
                action_id, constants.MINION_MACHINE_STATUS_IN_USE,
                refresh_allocation_time=True)

            # add any new machine entries to the DB:
            for new_machine in machine_db_entries_to_add:
                LOG.info(
                    "Adding new minion machine with ID '%s' to the DB for pool "
                    "'%s' for use with action '%s'.",
                    new_machine_id, minion_pool.id, action_id)
                db_api.add_minion_machine(ctxt, new_machine)
                new_machine_db_entries_added.append(new_machine.id)
        except Exception as ex:
            LOG.warn(
                "Exception occured while adding new minion machine entries to "
                "the DB for pool '%s' for use with action '%s'. Clearing "
                "any DB entries added so far (%s). Error was: %s",
                minion_pool.id, action_id,
                [m.id for m in new_machine_db_entries_added],
                utils.get_exception_details())
            try:
                LOG.debug(
                    "Reverting the following pre-existing minion machines from"
                    " pool '%s' to '%s' due to allocation error for action "
                    "'%s': %s",
                    minion_pool.id,
                    constants.MINION_MACHINE_STATUS_AVAILABLE,
                    action_id,
                    list(existing_machines_to_allocate.keys()))
                db_api.set_minion_machines_allocation_statuses(
                    ctxt, list(existing_machines_to_allocate.keys()),
                    None, constants.MINION_MACHINE_STATUS_AVAILABLE,
                    refresh_allocation_time=False)
            except Exception:
                LOG.warn(
                    "Failed to deallocate the following machines from pool "
                    "'%s' following allocation error for action '%s': %s. "
                    "Error trace was: %s",
                    minion_pool.id, action_id, existing_machines_to_allocate,
                    utils.get_exception_details())
            for new_machine in new_machine_db_entries_added:
                try:
                    db_api.delete_minion_machine(ctxt, new_machine.id)
                except Exception as ex:
                    LOG.warn(
                        "Error occured while removing minion machine entry "
                        "'%s' from the DB. This may leave the pool in an "
                        "inconsistent state. Error trace was: %s" % (
                            new_machine.id, utils.get_exception_details()))
                    continue
            raise

        LOG.debug(
            "The following minion machine allocation from pool '%s' were or "
            "will be made for action '%s': %s",
            minion_pool.id, action_id, instance_minion_allocations)
        return {
            "flow": allocation_subflow,
            "action_instance_minion_allocation_mappings": (
                instance_minion_allocations)}

    def _run_machine_allocation_subflow_for_action(
            self, ctxt, action, action_type, include_transfer_minions=True,
            include_osmorphing_minions=True):
        """ Defines and starts a taskflow subflow for allocating minion
        machines for the given action.
        If there are no more minion machines available, upscaling will occur.
        Also adds to the DB/marks as allocated any minion machines on the
        spot.
        """
        required_action_properties = [
            'id', 'instances', 'origin_minion_pool_id',
            'destination_minion_pool_id',
            'instance_osmorphing_minion_pool_mappings']
        self._check_keys_for_action_dict(
            action, required_action_properties,
            operation="minion machine selection")

        allocation_flow_name_format = None
        machines_allocation_subflow_name_format = None
        machine_action_allocation_subflow_name_format = None
        allocation_failure_reporting_task_class = None
        allocation_confirmation_reporting_task_class = None
        if action_type == constants.TRANSFER_ACTION_TYPE_MIGRATION:
            allocation_flow_name_format = (
                minion_manager_tasks.MINION_POOL_MIGRATION_ALLOCATION_FLOW_NAME_FORMAT)
            allocation_failure_reporting_task_class = (
                minion_manager_tasks.ReportMinionAllocationFailureForMigrationTask)
            allocation_confirmation_reporting_task_class = (
                minion_manager_tasks.ConfirmMinionAllocationForMigrationTask)
            machines_allocation_subflow_name_format = (
                minion_manager_tasks.MINION_POOL_MIGRATION_ALLOCATION_SUBFLOW_NAME_FORMAT)
            machine_action_allocation_subflow_name_format = (
                minion_manager_tasks.MINION_POOL_ALLOCATE_MACHINES_FOR_MIGRATION_SUBFLOW_NAME_FORMAT)
        elif action_type == constants.TRANSFER_ACTION_TYPE_REPLICA:
            allocation_flow_name_format = (
                minion_manager_tasks.MINION_POOL_REPLICA_ALLOCATION_FLOW_NAME_FORMAT)
            allocation_failure_reporting_task_class = (
                minion_manager_tasks.ReportMinionAllocationFailureForReplicaTask)
            allocation_confirmation_reporting_task_class = (
                minion_manager_tasks.ConfirmMinionAllocationForReplicaTask)
            machines_allocation_subflow_name_format = (
                minion_manager_tasks.MINION_POOL_REPLICA_ALLOCATION_SUBFLOW_NAME_FORMAT)
            machine_action_allocation_subflow_name_format = (
                minion_manager_tasks.MINION_POOL_ALLOCATE_MACHINES_FOR_REPLICA_SUBFLOW_NAME_FORMAT)
        else:
            raise exception.InvalidInput(
                "Unknown transfer action type '%s'" % action_type)

        # define main flow:
        main_allocation_flow_name = (
            allocation_flow_name_format % action['id'])
        main_allocation_flow = linear_flow.Flow(main_allocation_flow_name)
        instance_machine_allocations = {
            instance: {} for instance in action['instances']}

        # add allocation failure reporting task:
        main_allocation_flow.add(
            allocation_failure_reporting_task_class(
                action['id']))

        # define subflow for all the pool minions allocations:
        machines_subflow = unordered_flow.Flow(
            machines_allocation_subflow_name_format % action['id'])
        new_pools_machines_db_entries = {}

        # add subflow for origin pool:
        if include_transfer_minions and action['origin_minion_pool_id']:
            with minion_manager_utils.get_minion_pool_lock(
                    action['origin_minion_pool_id'], external=True):
                # fetch pool, origin endpoint, and initial store:
                minion_pool = self._get_minion_pool(
                    ctxt, action['origin_minion_pool_id'],
                    include_machines=True, include_events=False,
                    include_progress_updates=False)
                endpoint_dict = self._rpc_conductor_client.get_endpoint(
                    ctxt, minion_pool.endpoint_id)
                origin_pool_store = self._get_pool_initial_taskflow_store_base(
                    ctxt, minion_pool, endpoint_dict)

                # add subflow for machine allocations from origin pool:
                subflow_name = machine_action_allocation_subflow_name_format % (
                    minion_pool.id, action['id'])
                # NOTE: required to avoid internal taskflow conflicts
                subflow_name = "origin-%s" % subflow_name
                allocations_subflow_result = (
                    self._make_minion_machine_allocation_subflow_for_action(
                        ctxt, minion_pool, action['id'], action['instances'],
                        subflow_name, inject_for_tasks=origin_pool_store))
                machines_subflow.add(allocations_subflow_result['flow'])

                # register each instances' origin minion:
                source_machine_allocations = allocations_subflow_result[
                    'action_instance_minion_allocation_mappings']
                for (action_instance_id, allocated_minion_id) in (
                        source_machine_allocations.items()):
                    instance_machine_allocations[
                        action_instance_id]['origin_minion_id'] = (
                            allocated_minion_id)

        # add subflow for destination pool:
        if include_transfer_minions and action['destination_minion_pool_id']:
            with minion_manager_utils.get_minion_pool_lock(
                    action['destination_minion_pool_id'], external=True):
                # fetch pool, destination endpoint, and initial store:
                minion_pool = self._get_minion_pool(
                    ctxt, action['destination_minion_pool_id'],
                    include_machines=True, include_events=False,
                    include_progress_updates=False)
                endpoint_dict = self._rpc_conductor_client.get_endpoint(
                    ctxt, minion_pool.endpoint_id)
                destination_pool_store = (
                    self._get_pool_initial_taskflow_store_base(
                        ctxt, minion_pool, endpoint_dict))

                # add subflow for machine allocations from destination pool:
                subflow_name = machine_action_allocation_subflow_name_format % (
                    minion_pool.id, action['id'])
                # NOTE: required to avoid internal taskflow conflicts
                subflow_name = "destination-%s" % subflow_name
                allocations_subflow_result = (
                    self._make_minion_machine_allocation_subflow_for_action(
                        ctxt, minion_pool, action['id'], action['instances'],
                        subflow_name,
                        inject_for_tasks=destination_pool_store))
                machines_subflow.add(allocations_subflow_result['flow'])
                destination_machine_allocations = allocations_subflow_result[
                    'action_instance_minion_allocation_mappings']

                # register each instances' destination minion:
                for (action_instance_id, allocated_minion_id) in (
                        destination_machine_allocations.items()):
                    instance_machine_allocations[
                        action_instance_id]['destination_minion_id'] = (
                            allocated_minion_id)

        # add subflow for OSMorphing minions:
        osmorphing_pool_instance_mappings = {}
        for (action_instance_id, mapped_pool_id) in action[
                'instance_osmorphing_minion_pool_mappings'].items():
            if mapped_pool_id not in osmorphing_pool_instance_mappings:
                osmorphing_pool_instance_mappings[
                    mapped_pool_id] = [action_instance_id]
            else:
                osmorphing_pool_instance_mappings[mapped_pool_id].append(
                    action_instance_id)
        if include_osmorphing_minions and osmorphing_pool_instance_mappings:
            for (osmorphing_pool_id, action_instance_ids) in (
                    osmorphing_pool_instance_mappings.items()):
                # if the destination pool was selected as an OSMorphing pool
                # for any instances, we simply re-use all of the destination
                # minions for said instances:
                if action['destination_minion_pool_id'] and (
                        include_osmorphing_minions and (
                            osmorphing_pool_id == (
                                action['destination_minion_pool_id']))):
                    LOG.debug(
                        "Reusing destination minion pool with ID '%s' for the "
                        "following instances which had it selected as an "
                        "OSMorphing pool for action '%s': %s",
                        osmorphing_pool_id, action['id'], action_instance_ids)
                    for instance in action_instance_ids:
                        instance_machine_allocations[
                            instance]['osmorphing_minion_id'] = (
                                instance_machine_allocations[
                                    instance]['destination_minion_id'])
                    continue

                with minion_manager_utils.get_minion_pool_lock(
                        osmorphing_pool_id, external=True):
                    # fetch pool, destination endpoint, and initial store:
                    minion_pool = self._get_minion_pool(
                        ctxt, osmorphing_pool_id,
                        include_machines=True, include_events=False,
                        include_progress_updates=False)
                    endpoint_dict = self._rpc_conductor_client.get_endpoint(
                        ctxt, minion_pool.endpoint_id)
                    osmorphing_pool_store = self._get_pool_initial_taskflow_store_base(
                        ctxt, minion_pool, endpoint_dict)

                    # add subflow for machine allocations from osmorphing pool:
                    subflow_name = machine_action_allocation_subflow_name_format % (
                        minion_pool.id, action['id'])
                    # NOTE: required to avoid internal taskflow conflicts
                    subflow_name = "osmorphing-%s" % subflow_name
                    allocations_subflow_result = (
                        self._make_minion_machine_allocation_subflow_for_action(
                            ctxt, minion_pool, action['id'],
                            action_instance_ids,
                            subflow_name, inject_for_tasks=osmorphing_pool_store))
                    machines_subflow.add(allocations_subflow_result['flow'])

                    # register each instances' osmorphing minion:
                    osmorphing_machine_allocations = allocations_subflow_result[
                        'action_instance_minion_allocation_mappings']
                    for (action_instance_id, allocated_minion_id) in (
                            osmorphing_machine_allocations.items()):
                        instance_machine_allocations[
                            action_instance_id]['osmorphing_minion_id'] = (
                                allocated_minion_id)

        # add the machines subflow to the main flow:
        main_allocation_flow.add(machines_subflow)

        # add final task to report minion machine availablity
        # to the conductor at the end of the flow:
        main_allocation_flow.add(
            allocation_confirmation_reporting_task_class(
                action['id'], instance_machine_allocations))

        LOG.info(
            "Starting main minion allocation flow '%s' for with ID '%s'. "
            "The minion allocations will be: %s" % (
                main_allocation_flow_name, action['id'],
                instance_machine_allocations))

        self._taskflow_runner.run_flow_in_background(
            main_allocation_flow, store={"context": ctxt})

        return main_allocation_flow

    def _cleanup_machines_with_statuses_for_action(
            self, ctxt, action_id, targeted_statuses, exclude_pools=None):
        """ Deletes all minion machines which are marked with the given
        from the DB.
        """
        if exclude_pools is None:
            exclude_pools = []
        machines = db_api.get_minion_machines(ctxt, action_id)
        if not machines:
            LOG.debug(
                "No minion machines allocated to action '%s'. Returning.",
                action_id)
            return

        pool_machine_mappings = {}
        for machine in machines:
            if machine.status not in targeted_statuses:
                LOG.debug(
                    "Skipping deletion of machine '%s' from pool '%s' as "
                    "its status (%s) is not one of the targeted statuses (%s)",
                    machine.id, machine.pool_id, machine.status,
                    targeted_statuses)
                continue
            if machine.pool_id in exclude_pools:
                LOG.debug(
                    "Skipping deletion of machine '%s' (status '%s') from "
                    "whitelisted pool '%s'", machine.id, machine.status,
                    machine.pool_id)
                continue

            if machine.pool_id not in pool_machine_mappings:
                pool_machine_mappings[machine.pool_id] = [machine]
            else:
                pool_machine_mappings[machine.pool_id].append(machine)

        for (pool_id, machines) in pool_machine_mappings.items():
            with minion_manager_utils.get_minion_pool_lock(
                   pool_id, external=True):
                for machine in machines:
                    LOG.debug(
                        "Deleting machine with ID '%s' (pool '%s', status '%s') "
                        "from the DB.", machine.id, pool_id, machine.status)
                    db_api.delete_minion_machine(ctxt, machine.id)

    def deallocate_minion_machine(self, ctxt, minion_machine_id):

        minion_machine = db_api.get_minion_machine(
            ctxt, minion_machine_id)
        if not minion_machine:
            LOG.warn(
                "Could not find minion machine with ID '%s' for deallocation. "
                "Presuming it was deleted and returning early",
                minion_machine_id)
            return

        machine_allocated_status = constants.MINION_MACHINE_STATUS_IN_USE
        with minion_manager_utils.get_minion_pool_lock(
                minion_machine.pool_id, external=True):
            if minion_machine.status != machine_allocated_status or (
                    not minion_machine.allocated_action):
                LOG.warn(
                    "Minion machine '%s' was either in an improper status (%s)"
                    ", or did not have an associated action ('%s') for "
                    "deallocation request. Marking as available anyway.",
                    minion_machine.id, minion_machine.status,
                    minion_machine.allocated_action)
            LOG.debug(
                "Attempting to deallocate all minion pool machine '%s' "
                "(currently allocated to action '%s' with status '%s')",
                minion_machine.id, minion_machine.allocated_action,
                minion_machine.status)
            db_api.update_minion_machine(
                ctxt, minion_machine.id, {
                    "status": constants.MINION_MACHINE_STATUS_AVAILABLE,
                    "allocated_action": None})
            LOG.debug(
                "Successfully deallocated minion machine with '%s'.",
                minion_machine.id)

    def deallocate_minion_machines_for_action(self, ctxt, action_id):

        allocated_minion_machines = db_api.get_minion_machines(
            ctxt, allocated_action_id=action_id)

        if not allocated_minion_machines:
            LOG.debug(
                "No minion machines seem to have been used for action with "
                "base_id '%s'. Skipping minion machine deallocation.",
                action_id)
            return

        # categorise machine objects by pool:
        pool_machine_mappings = {}
        for machine in allocated_minion_machines:
            if machine.pool_id not in pool_machine_mappings:
                pool_machine_mappings[machine.pool_id] = []
            pool_machine_mappings[machine.pool_id].append(machine)

        # iterate over each pool and its machines allocated to this action:
        for (pool_id, pool_machines) in pool_machine_mappings.items():
            with minion_manager_utils.get_minion_pool_lock(
                    pool_id, external=True):
                machine_ids_to_deallocate = []
                # NOTE: this is a workaround in case some crash/restart happens
                # in the minion-manager service while new machine DB entries
                # are added to the DB without their point of deployment being
                # reached for them to ever get out of 'UNINITIALIZED' status:
                for machine in pool_machines:
                    if machine.status == (
                            constants.MINION_MACHINE_STATUS_UNINITIALIZED):
                        LOG.warn(
                            "Found minion machine '%s' in pool '%s' which "
                            "is in '%s' status. Removing from the DB "
                            "entirely." % (
                                machine.id, pool_id, machine.status))
                        db_api.delete_minion_machine(
                            ctxt, machine.id)
                        LOG.info(
                            "Successfully deleted minion machine entry '%s' "
                            "from pool '%s' from the DB.", machine.id, pool_id)
                        continue
                    LOG.debug(
                        "Going to mark minion machine '%s' (current status "
                        "'%s') of pool '%s' as available following machine "
                        "deallocation request for action '%s'.",
                        machine.id, machine.status, pool_id, action_id)
                    machine_ids_to_deallocate.append(machine.id)

                LOG.info(
                    "Marking minion machines '%s' from pool '%s' for "
                    "as available after having been allocated to action '%s'.",
                    machine_ids_to_deallocate, pool_id, action_id)
                db_api.set_minion_machines_allocation_statuses(
                    ctxt, machine_ids_to_deallocate, None,
                    constants.MINION_MACHINE_STATUS_AVAILABLE,
                    refresh_allocation_time=False)

        LOG.debug(
            "Successfully released all minion machines associated "
            "with action with base_id '%s'.", action_id)

    def _get_healtchcheck_flow_for_minion_machine(
            self, minion_pool, minion_machine_id, allocate_to_action=None,
            machine_status_on_success=constants.MINION_MACHINE_STATUS_AVAILABLE,
            inject_for_tasks=None):
        """ Returns a taskflow graph flow with a healtcheck task
        and redeployment subflow on error. """
        # define healthcheck subflow for each machine:
        machine_healthcheck_subflow = graph_flow.Flow(
            minion_manager_tasks.MINION_POOL_HEALTHCHECK_MACHINE_SUBFLOW_NAME_FORMAT % (
                minion_pool.id, minion_machine_id))

        # add healtcheck task to healthcheck subflow:
        machine_healthcheck_task = (
            minion_manager_tasks.HealthcheckMinionMachineTask(
                minion_pool.id, minion_machine_id, minion_pool.platform,
                machine_status_on_success=machine_status_on_success,
                fail_on_error=False, inject=inject_for_tasks))
        machine_healthcheck_subflow.add(machine_healthcheck_task)

        # define reallocation subflow:
        machine_reallocation_subflow = linear_flow.Flow(
            minion_manager_tasks.MINION_POOL_REALLOCATE_MACHINE_SUBFLOW_NAME_FORMAT % (
                minion_pool.id, minion_machine_id))
        machine_reallocation_subflow.add(
            minion_manager_tasks.DeallocateMinionMachineTask(
                minion_pool.id, minion_machine_id, minion_pool.platform,
                inject=inject_for_tasks))
        machine_reallocation_subflow.add(
            minion_manager_tasks.AllocateMinionMachineTask(
                minion_pool.id, minion_machine_id, minion_pool.platform,
                allocate_to_action=allocate_to_action,
                inject=inject_for_tasks))
        machine_healthcheck_subflow.add(
            machine_reallocation_subflow,
            # NOTE: this is required to not have taskflow attempt (and fail)
            # to automatically link the above Healthcheck task to the
            # new subflow based on inputs/outputs alone:
            resolve_existing=False)

        # link reallocation subflow to healthcheck task:
        machine_healthcheck_subflow.link(
            machine_healthcheck_task, machine_reallocation_subflow,
            # NOTE: this is required to prevent any parent flows from skipping:
            decider_depth=taskflow_deciders.Depth.FLOW,
            decider=minion_manager_tasks.MinionMachineHealtchcheckDecider(
                minion_pool.id, minion_machine_id,
                on_successful_healthcheck=False))

        return machine_healthcheck_subflow

    def _get_minion_pool_refresh_flow(
            self, ctxt, minion_pool, requery=True):

        if requery:
            minion_pool = self._get_minion_pool(
                ctxt, minion_pool.id, include_machines=True,
                include_progress_updates=False, include_events=False)

        pool_refresh_flow = unordered_flow.Flow(
            minion_manager_tasks.MINION_POOL_REFRESH_FLOW_NAME_FORMAT % (
                minion_pool.id))
        max_minions_to_deallocate = (
            len(minion_pool.minion_machines) - minion_pool.minimum_minions)
        now = timeutils.utcnow()
        machines_to_deallocate = []
        machines_to_healthcheck = []
        skipped_machines = {}

        for machine in minion_pool.minion_machines:
            if machine.status != constants.MINION_MACHINE_STATUS_AVAILABLE:
                skipped_machines[machine.id] = machine.status
                continue

            minion_expired = True
            if machine.last_used_at:
                expiry_time = (
                    machine.last_used_at + datetime.timedelta(
                        seconds=minion_pool.minion_max_idle_time))
                minion_expired = expiry_time <= now

            # deallocate the machine if it is expired:
            if max_minions_to_deallocate > 0 and minion_expired:
                pool_refresh_flow.add(
                    minion_manager_tasks.DeallocateMinionMachineTask(
                        minion_pool.id, machine.id, minion_pool.platform))
                max_minions_to_deallocate = max_minions_to_deallocate - 1
                machines_to_deallocate.append(machine.id)
            # else, perform a healthcheck on the machine:
            else:
                pool_refresh_flow.add(
                    self._get_healtchcheck_flow_for_minion_machine(
                        minion_pool, machine.id, allocate_to_action=None,
                        machine_status_on_success=(
                            constants.MINION_MACHINE_STATUS_AVAILABLE)))
                machines_to_healthcheck.append(machine.id)

        # update DB entried for all machines:
        if machines_to_deallocate:
            LOG.debug(
                "The following minion machines will be deallocated as part "
                "of the refreshing of minion pool '%s': %s",
                minion_pool.id, machines_to_deallocate)
            for machine in machines_to_deallocate:
                db_api.set_minion_machine_status(
                    ctxt, machine,
                    constants.MINION_MACHINE_STATUS_DEALLOCATING)
        if machines_to_healthcheck:
            LOG.debug(
                "The following minion machines will be healthchecked as part "
                "of the refreshing of minion pool '%s': %s",
                minion_pool.id, machines_to_healthcheck)
            for machine in machines_to_healthcheck:
                db_api.set_minion_machine_status(
                    ctxt, machine,
                    constants.MINION_MACHINE_STATUS_HEALTHCHECKING)
        if skipped_machines:
            LOG.debug(
                "The following minion machines were skipped during the "
                "refreshing of minion pool '%s' as they were in other "
                "statuses than the serviceable ones: %s",
                minion_pool.id, skipped_machines)

        return pool_refresh_flow

    @minion_manager_utils.minion_pool_synchronized_op
    def refresh_minion_pool(self, ctxt, minion_pool_id):
        LOG.info("Attempting to healthcheck Minion Pool '%s'.", minion_pool_id)
        minion_pool = self._get_minion_pool(
            ctxt, minion_pool_id, include_events=False, include_machines=True,
            include_progress_updates=False)
        endpoint_dict = self._rpc_conductor_client.get_endpoint(
            ctxt, minion_pool.endpoint_id)
        acceptable_allocation_statuses = [
            constants.MINION_POOL_STATUS_ALLOCATED]
        current_status = minion_pool.status
        if current_status not in acceptable_allocation_statuses:
            raise exception.InvalidMinionPoolState(
                "Minion machines for pool '%s' cannot be healthchecked as the "
                "pool is in '%s' state instead of the expected %s." % (
                    minion_pool_id, current_status,
                    acceptable_allocation_statuses))

        healthcheck_flow = self._get_minion_pool_refresh_flow(
            ctxt, minion_pool, requery=False)
        if not healthcheck_flow:
            msg = (
                "There are no minion machine healthchecks to be performed at "
                "this time." % minion_pool_id)
            LOG.debug(msg)
            db_api.add_minion_pool_event(
                ctxt, minion_pool.id, constants.TASK_EVENT_INFO, msg)
            return self._get_minion_pool(ctxt, minion_pool.id)

        initial_store = self._get_pool_initial_taskflow_store_base(
            ctxt, minion_pool, endpoint_dict)
        self._taskflow_runner.run_flow_in_background(
            healthcheck_flow, store=initial_store)

        return self._get_minion_pool(ctxt, minion_pool.id)

    def _get_minion_pool_allocation_flow(self, minion_pool):
        """ Returns a taskflow.Flow object pertaining to all the tasks
        required for allocating a minion pool (validation, shared resource
        setup, and actual minion creation)
        """
        # create task flow:
        allocation_flow = linear_flow.Flow(
            minion_manager_tasks.MINION_POOL_ALLOCATION_FLOW_NAME_FORMAT % (
                minion_pool.id))

        # tansition pool to VALIDATING:
        allocation_flow.add(minion_manager_tasks.UpdateMinionPoolStatusTask(
            minion_pool.id, constants.MINION_POOL_STATUS_VALIDATING_INPUTS,
            status_to_revert_to=constants.MINION_POOL_STATUS_ERROR))

        # add pool options validation task:
        allocation_flow.add(minion_manager_tasks.ValidateMinionPoolOptionsTask(
            # NOTE: we pass in the ID of the minion pool itself as both
            # the task ID and the instance ID for tasks which are strictly
            # pool-related.
            minion_pool.id,
            minion_pool.id,
            minion_pool.platform))

        # transition pool to 'DEPLOYING_SHARED_RESOURCES':
        allocation_flow.add(minion_manager_tasks.UpdateMinionPoolStatusTask(
            minion_pool.id,
            constants.MINION_POOL_STATUS_ALLOCATING_SHARED_RESOURCES))

        # add pool shared resources deployment task:
        allocation_flow.add(
            minion_manager_tasks.AllocateSharedPoolResourcesTask(
                minion_pool.id, minion_pool.id, minion_pool.platform,
                # NOTE: the shared resource deployment task will always get
                # run by itself so it is safe to have it override task_info:
                provides='task_info'))

        # add subflow for deploying all of the minion machines:
        fmt = (
            minion_manager_tasks.MINION_POOL_ALLOCATE_MINIONS_SUBFLOW_NAME_FORMAT)
        machines_flow = unordered_flow.Flow(fmt % minion_pool.id)
        pool_machine_ids = []
        for _ in range(minion_pool.minimum_minions):
            machine_id = str(uuid.uuid4())
            pool_machine_ids.append(machine_id)
            machines_flow.add(
                minion_manager_tasks.AllocateMinionMachineTask(
                    minion_pool.id, machine_id, minion_pool.platform))
        # NOTE: bool(flow) == False if the flow has no child flows/tasks:
        if machines_flow:
            allocation_flow.add(minion_manager_tasks.UpdateMinionPoolStatusTask(
                minion_pool.id,
                constants.MINION_POOL_STATUS_ALLOCATING_MACHINES))
            LOG.debug(
                "The following minion machine IDs will be created for "
                "pool with ID '%s': %s" % (minion_pool.id, pool_machine_ids))
            allocation_flow.add(machines_flow)
        else:
            LOG.debug(
                "No upfront minion machine deployments required for minion "
                "pool with ID '%s'", minion_pool.id)

        # transition pool to ALLOCATED:
        allocation_flow.add(minion_manager_tasks.UpdateMinionPoolStatusTask(
            minion_pool.id, constants.MINION_POOL_STATUS_ALLOCATED))

        return allocation_flow


    def create_minion_pool(
            self, ctxt, name, endpoint_id, pool_platform, pool_os_type,
            environment_options, minimum_minions, maximum_minions,
            minion_max_idle_time, minion_retention_strategy, notes=None,
            skip_allocation=False):

        endpoint_dict = self._rpc_conductor_client.get_endpoint(
            ctxt, endpoint_id)
        minion_pool = models.MinionPool()
        minion_pool.id = str(uuid.uuid4())
        minion_pool.name = name
        minion_pool.notes = notes
        minion_pool.platform = pool_platform
        minion_pool.os_type = pool_os_type
        minion_pool.endpoint_id = endpoint_id
        minion_pool.environment_options = environment_options
        minion_pool.status = constants.MINION_POOL_STATUS_DEALLOCATED
        minion_pool.minimum_minions = minimum_minions
        minion_pool.maximum_minions = maximum_minions
        minion_pool.minion_max_idle_time = minion_max_idle_time
        minion_pool.minion_retention_strategy = minion_retention_strategy

        db_api.add_minion_pool(ctxt, minion_pool)

        if not skip_allocation:
            allocation_flow = self._get_minion_pool_allocation_flow(
                minion_pool)
            # start the deployment flow:
            initial_store = self._get_pool_initial_taskflow_store_base(
                ctxt, minion_pool, endpoint_dict)
            self._taskflow_runner.run_flow_in_background(
                allocation_flow, store=initial_store)

        return self.get_minion_pool(ctxt, minion_pool.id)

    def _get_pool_initial_taskflow_store_base(
            self, ctxt, minion_pool, endpoint_dict):
        # NOTE: considering pools are associated to strictly one endpoint,
        # we can duplicate the 'origin/destination':
        origin_info = {
            "id": endpoint_dict['id'],
            "connection_info": endpoint_dict['connection_info'],
            "mapped_regions": endpoint_dict['mapped_regions'],
            "type": endpoint_dict['type']}
        initial_store = {
            "context": ctxt,
            "origin": origin_info,
            "destination": origin_info,
            "task_info": {
                "pool_identifier": minion_pool.id,
                "pool_os_type": minion_pool.os_type,
                "pool_environment_options": minion_pool.environment_options}}
        shared_resources = minion_pool.shared_resources
        if shared_resources is None:
            shared_resources = {}
        initial_store['task_info']['pool_shared_resources'] = shared_resources
        return initial_store

    def _check_pool_machines_in_use(
            self, ctxt, minion_pool, raise_if_in_use=False, requery=False):
        """ Checks whether the given pool has any machines currently in-use.
        Returns a list of the used machines if so, or an empty list of not.
        """
        if requery:
            minion_pool = self._get_minion_pool(
                ctxt, minion_pool.id, include_machines=True,
                include_events=False, include_progress_updates=False)
        unused_machine_states = [
            constants.MINION_MACHINE_STATUS_AVAILABLE,
            constants.MINION_MACHINE_STATUS_ERROR_DEPLOYING,
            constants.MINION_MACHINE_STATUS_ERROR]
        used_machines = {
            mch for mch in minion_pool.minion_machines
            if mch.status not in unused_machine_states}
        if used_machines and raise_if_in_use:
            raise exception.InvalidMinionPoolState(
                "Minion pool '%s' has one or more machines which are in an"
                " active state: %s" % (
                    minion_pool.id, {
                        mch.id: mch.status for mch in used_machines}))
        return used_machines

    @minion_manager_utils.minion_pool_synchronized_op
    def allocate_minion_pool(self, ctxt, minion_pool_id):
        LOG.info("Attempting to allocate Minion Pool '%s'.", minion_pool_id)
        minion_pool = self._get_minion_pool(
            ctxt, minion_pool_id, include_events=False, include_machines=False,
            include_progress_updates=False)
        endpoint_dict = self._rpc_conductor_client.get_endpoint(
            ctxt, minion_pool.endpoint_id)
        acceptable_allocation_statuses = [
            constants.MINION_POOL_STATUS_DEALLOCATED]
        current_status = minion_pool.status
        if current_status not in acceptable_allocation_statuses:
            raise exception.InvalidMinionPoolState(
                "Minion machines for pool '%s' cannot be allocated as the pool"
                " is in '%s' state instead of the expected %s. Please "
                "force-deallocate the pool and try again." % (
                    minion_pool_id, minion_pool.status,
                    acceptable_allocation_statuses))

        allocation_flow = self._get_minion_pool_allocation_flow(minion_pool)
        initial_store = self._get_pool_initial_taskflow_store_base(
            ctxt, minion_pool, endpoint_dict)

        try:
            db_api.set_minion_pool_status(
                ctxt, minion_pool_id,
                constants.MINION_POOL_STATUS_POOL_MAINTENANCE)
            self._taskflow_runner.run_flow_in_background(
                allocation_flow, store=initial_store)
        except:
            db_api.set_minion_pool_status(
                ctxt, minion_pool_id, current_status)
            raise

        return self._get_minion_pool(ctxt, minion_pool.id)

    def _get_minion_pool_deallocation_flow(self, minion_pool):
        """ Returns a taskflow.Flow object pertaining to all the tasks
        required for deallocating a minion pool (machines and shared resources)
        """
        # create task flow:
        deallocation_flow = linear_flow.Flow(
            minion_manager_tasks.MINION_POOL_DEALLOCATION_FLOW_NAME_FORMAT % (
                minion_pool.id))

        # add subflow for deallocating all of the minion machines:
        fmt = (
            minion_manager_tasks.MINION_POOL_DEALLOCATE_MACHINES_SUBFLOW_NAME_FORMAT)
        machines_flow = unordered_flow.Flow(fmt % minion_pool.id)
        for machine in minion_pool.minion_machines:
            machines_flow.add(
                minion_manager_tasks.DeallocateMinionMachineTask(
                    minion_pool.id, machine.id, minion_pool.platform))
        # NOTE: bool(flow) == False if the flow has no child flows/tasks:
        if machines_flow:
            # tansition pool to DEALLOCATING_MACHINES:
            deallocation_flow.add(minion_manager_tasks.UpdateMinionPoolStatusTask(
                minion_pool.id,
                constants.MINION_POOL_STATUS_DEALLOCATING_MACHINES,
                status_to_revert_to=constants.MINION_POOL_STATUS_ERROR))
            deallocation_flow.add(machines_flow)
        else:
            LOG.debug(
                "No machines for pool '%s' require deallocating.", minion_pool.id)

        # transition pool to DEALLOCATING_SHARED_RESOURCES:
        deallocation_flow.add(minion_manager_tasks.UpdateMinionPoolStatusTask(
            minion_pool.id,
            constants.MINION_POOL_STATUS_DEALLOCATING_SHARED_RESOURCES,
            status_to_revert_to=constants.MINION_POOL_STATUS_ERROR))

        # add pool shared resources deletion task:
        deallocation_flow.add(
            minion_manager_tasks.DeallocateSharedPoolResourcesTask(
                minion_pool.id, minion_pool.id, minion_pool.platform))

        # transition pool to DEALLOCATED:
        deallocation_flow.add(minion_manager_tasks.UpdateMinionPoolStatusTask(
            minion_pool.id, constants.MINION_POOL_STATUS_DEALLOCATED))

        return deallocation_flow

    def _get_pool_deallocation_initial_store(
            self, ctxt, minion_pool, endpoint_dict):
        base = self._get_pool_initial_taskflow_store_base(
            ctxt, minion_pool, endpoint_dict)
        if 'task_info' not in base:
            base['task_info'] = {}
        base['task_info']['pool_shared_resources'] = (
            minion_pool.shared_resources)
        return base

    @minion_manager_utils.minion_pool_synchronized_op
    def deallocate_minion_pool(self, ctxt, minion_pool_id, force=False):
        LOG.info("Attempting to deallocate Minion Pool '%s'.", minion_pool_id)
        minion_pool = self._get_minion_pool(
            ctxt, minion_pool_id, include_events=False, include_machines=True,
            include_progress_updates=False)
        current_status = minion_pool.status
        if current_status == constants.MINION_POOL_STATUS_DEALLOCATED:
            LOG.debug(
                "Deallocation requested on already deallocated pool '%s'. "
                "Nothing to do so returning early.", minion_pool_id)
            return self._get_minion_pool(ctxt, minion_pool.id)
        acceptable_deallocation_statuses = [
            constants.MINION_POOL_STATUS_ALLOCATED,
            constants.MINION_POOL_STATUS_ERROR]
        if current_status not in acceptable_deallocation_statuses:
            if not force:
                raise exception.InvalidMinionPoolState(
                    "Minion pool '%s' cannot be deallocated as the pool"
                    " is in '%s' state instead of one of the expected %s"% (
                        minion_pool_id, minion_pool.status,
                        acceptable_deallocation_statuses))
            else:
                LOG.warn(
                    "Forcibly deallocating minion pool '%s' at user request.",
                    minion_pool_id)
        self._check_pool_machines_in_use(
            ctxt, minion_pool, raise_if_in_use=not force)
        endpoint_dict = self._rpc_conductor_client.get_endpoint(
            ctxt, minion_pool.endpoint_id)

        deallocation_flow = self._get_minion_pool_deallocation_flow(
            minion_pool)
        initial_store = self._get_pool_deallocation_initial_store(
            ctxt, minion_pool, endpoint_dict)

        try:
            db_api.set_minion_pool_status(
                ctxt, minion_pool_id,
                constants.MINION_POOL_STATUS_POOL_MAINTENANCE)
            self._taskflow_runner.run_flow_in_background(
                deallocation_flow, store=initial_store)
        except:
            db_api.set_minion_pool_status(
                ctxt, minion_pool_id, current_status)
            raise

        return self._get_minion_pool(ctxt, minion_pool.id)

    def get_minion_pools(self, ctxt, include_machines=True):
        return db_api.get_minion_pools(
            ctxt, include_machines=include_machines, include_events=False,
            include_progress_updates=False)

    def _get_minion_pool(
            self, ctxt, minion_pool_id, include_machines=True,
            include_events=True, include_progress_updates=True):
        minion_pool = db_api.get_minion_pool(
            ctxt, minion_pool_id, include_machines=include_machines,
            include_events=include_events,
            include_progress_updates=include_progress_updates)
        if not minion_pool:
            raise exception.NotFound(
                "Minion pool with ID '%s' not found." % minion_pool_id)
        return minion_pool

    # @minion_manager_utils.minion_pool_synchronized_op
    # def set_up_shared_minion_pool_resources(self, ctxt, minion_pool_id):
    #     LOG.info(
    #         "Attempting to set up shared resources for Minion Pool '%s'.",
    #         minion_pool_id)
    #     minion_pool = db_api.get_minion_pool_lifecycle(
    #         ctxt, minion_pool_id, include_tasks_executions=False,
    #         include_machines=False)
    #     if minion_pool.status != constants.MINION_POOL_STATUS_UNINITIALIZED:
    #         raise exception.InvalidMinionPoolState(
    #             "Minion Pool '%s' cannot have shared resources set up as it "
    #             "is in '%s' state instead of the expected %s."% (
    #                 minion_pool_id, minion_pool.status,
    #                 constants.MINION_POOL_STATUS_UNINITIALIZED))

    #     execution = models.TasksExecution()
    #     execution.id = str(uuid.uuid4())
    #     execution.action = minion_pool
    #     execution.status = constants.EXECUTION_STATUS_UNEXECUTED
    #     execution.type = (
    #         constants.EXECUTION_TYPE_MINION_POOL_SET_UP_SHARED_RESOURCES)

    #     minion_pool.info[minion_pool_id] = {
    #         "pool_os_type": minion_pool.os_type,
    #         "pool_identifier": minion_pool.id,
    #         # TODO(aznashwan): remove redundancy once transfer
    #         # action DB models have been overhauled:
    #         "pool_environment_options": minion_pool.source_environment}

    #     validate_task_type = (
    #         constants.TASK_TYPE_VALIDATE_DESTINATION_MINION_POOL_OPTIONS)
    #     set_up_task_type = (
    #         constants.TASK_TYPE_SET_UP_DESTINATION_POOL_SHARED_RESOURCES)
    #     if minion_pool.platform == constants.PROVIDER_PLATFORM_SOURCE:
    #         validate_task_type = (
    #             constants.TASK_TYPE_VALIDATE_SOURCE_MINION_POOL_OPTIONS)
    #         set_up_task_type = (
    #             constants.TASK_TYPE_SET_UP_SOURCE_POOL_SHARED_RESOURCES)

    #     validate_pool_options_task = self._create_task(
    #         minion_pool.id, validate_task_type, execution)

    #     setup_pool_resources_task = self._create_task(
    #         minion_pool.id,
    #         set_up_task_type,
    #         execution,
    #         depends_on=[validate_pool_options_task.id])

    #     self._check_execution_tasks_sanity(execution, minion_pool.info)

    #     # update the action info for the pool's instance:
    #     db_api.update_transfer_action_info_for_instance(
    #         ctxt, minion_pool.id, minion_pool.id,
    #         minion_pool.info[minion_pool.id])

    #     # add new execution to DB:
    #     db_api.add_minion_pool_lifecycle_execution(ctxt, execution)
    #     LOG.info(
    #         "Minion pool shared resource creation execution created: %s",
    #         execution.id)

    #     self._begin_tasks(ctxt, minion_pool, execution)
    #     db_api.set_minion_pool_lifecycle_status(
    #         ctxt, minion_pool.id, constants.MINION_POOL_STATUS_INITIALIZING)

    #     return self._get_minion_pool_lifecycle_execution(
    #         ctxt, minion_pool_id, execution.id).to_dict()

    # @minion_manager_utils.minion_pool_synchronized_op
    # def tear_down_shared_minion_pool_resources(
    #         self, ctxt, minion_pool_id, force=False):
    #     minion_pool = db_api.get_minion_pool_lifecycle(
    #         ctxt, minion_pool_id, include_tasks_executions=False,
    #         include_machines=False)
    #     if minion_pool.status != (
    #             constants.MINION_POOL_STATUS_DEALLOCATED) and not force:
    #         raise exception.InvalidMinionPoolState(
    #             "Minion Pool '%s' cannot have shared resources torn down as it"
    #             " is in '%s' state instead of the expected %s. "
    #             "Please use the force flag if you are certain you want "
    #             "to tear down the shared resources for this pool." % (
    #                 minion_pool_id, minion_pool.status,
    #                 constants.MINION_POOL_STATUS_DEALLOCATED))

    #     LOG.info(
    #         "Attempting to tear down shared resources for Minion Pool '%s'.",
    #         minion_pool_id)

    #     execution = models.TasksExecution()
    #     execution.id = str(uuid.uuid4())
    #     execution.action = minion_pool
    #     execution.status = constants.EXECUTION_STATUS_UNEXECUTED
    #     execution.type = (
    #         constants.EXECUTION_TYPE_MINION_POOL_TEAR_DOWN_SHARED_RESOURCES)

    #     tear_down_task_type = (
    #         constants.TASK_TYPE_TEAR_DOWN_DESTINATION_POOL_SHARED_RESOURCES)
    #     if minion_pool.platform == constants.PROVIDER_PLATFORM_SOURCE:
    #         tear_down_task_type = (
    #             constants.TASK_TYPE_TEAR_DOWN_SOURCE_POOL_SHARED_RESOURCES)

    #     self._create_task(
    #         minion_pool.id, tear_down_task_type, execution)

    #     self._check_execution_tasks_sanity(execution, minion_pool.info)

    #     # update the action info for the pool's instance:
    #     db_api.update_transfer_action_info_for_instance(
    #         ctxt, minion_pool.id, minion_pool.id,
    #         minion_pool.info[minion_pool.id])

    #     # add new execution to DB:
    #     db_api.add_minion_pool_lifecycle_execution(ctxt, execution)
    #     LOG.info(
    #         "Minion pool shared resource teardown execution created: %s",
    #         execution.id)

    #     self._begin_tasks(ctxt, minion_pool, execution)
    #     db_api.set_minion_pool_lifecycle_status(
    #         ctxt, minion_pool.id, constants.MINION_POOL_STATUS_UNINITIALIZING)

    #     return self._get_minion_pool_lifecycle_execution(
    #         ctxt, minion_pool_id, execution.id).to_dict()

    # @minion_manager_utils.minion_pool_synchronized_op
    # def allocate_minion_pool_machines(self, ctxt, minion_pool_id):
    #     LOG.info("Attempting to allocate Minion Pool '%s'.", minion_pool_id)
    #     minion_pool = self._get_minion_pool(
    #         ctxt, minion_pool_id, include_tasks_executions=False,
    #         include_machines=True)
    #     if minion_pool.status != constants.MINION_POOL_STATUS_DEALLOCATED:
    #         raise exception.InvalidMinionPoolState(
    #             "Minion machines for pool '%s' cannot be allocated as the pool"
    #             " is in '%s' state instead of the expected %s."% (
    #                 minion_pool_id, minion_pool.status,
    #                 constants.MINION_POOL_STATUS_DEALLOCATED))

    #     execution = models.TasksExecution()
    #     execution.id = str(uuid.uuid4())
    #     execution.action = minion_pool
    #     execution.status = constants.EXECUTION_STATUS_UNEXECUTED
    #     execution.type = constants.EXECUTION_TYPE_MINION_POOL_ALLOCATE_MINIONS

    #     new_minion_machine_ids = [
    #         str(uuid.uuid4()) for _ in range(minion_pool.minimum_minions)]

    #     create_minion_task_type = (
    #         constants.TASK_TYPE_CREATE_DESTINATION_MINION_MACHINE)
    #     delete_minion_task_type = (
    #         constants.TASK_TYPE_DELETE_DESTINATION_MINION_MACHINE)
    #     if minion_pool.platform == constants.PROVIDER_PLATFORM_SOURCE:
    #         create_minion_task_type = (
    #             constants.TASK_TYPE_CREATE_SOURCE_MINION_MACHINE)
    #         delete_minion_task_type = (
    #             constants.TASK_TYPE_DELETE_DESTINATION_MINION_MACHINE)

    #     for minion_machine_id in new_minion_machine_ids:
    #         minion_pool.info[minion_machine_id] = {
    #             "pool_identifier": minion_pool_id,
    #             "pool_os_type": minion_pool.os_type,
    #             "pool_shared_resources": minion_pool.shared_resources,
    #             "pool_environment_options": minion_pool.source_environment,
    #             # NOTE: we default this to an empty dict here to avoid possible
    #             # task info conflicts on the cleanup task below for minions
    #             # which were slower to deploy:
    #             "minion_provider_properties": {}}

    #         create_minion_task = self._create_task(
    #             minion_machine_id, create_minion_task_type, execution)

    #         self._create_task(
    #             minion_machine_id,
    #             delete_minion_task_type,
    #             execution, on_error_only=True,
    #             depends_on=[create_minion_task.id])

    #     self._check_execution_tasks_sanity(execution, minion_pool.info)

    #     # update the action info for all of the pool's minions:
    #     for minion_machine_id in new_minion_machine_ids:
    #         db_api.update_transfer_action_info_for_instance(
    #             ctxt, minion_pool.id, minion_machine_id,
    #             minion_pool.info[minion_machine_id])

    #     # add new execution to DB:
    #     db_api.add_minion_pool_lifecycle_execution(ctxt, execution)
    #     LOG.info("Minion pool allocation execution created: %s", execution.id)

    #     self._begin_tasks(ctxt, minion_pool, execution)
    #     db_api.set_minion_pool_lifecycle_status(
    #         ctxt, minion_pool.id, constants.MINION_POOL_STATUS_ALLOCATING)

    #     return self._get_minion_pool_lifecycle_execution(
    #         ctxt, minion_pool_id, execution.id).to_dict()

    # def _check_all_pool_minion_machines_available(self, minion_pool):
    #     if not minion_pool.minion_machines:
    #         LOG.debug(
    #             "Minion pool '%s' does not have any allocated machines.",
    #             minion_pool.id)
    #         return

    #     allocated_machine_statuses = {
    #         machine.id: machine.status
    #         for machine in minion_pool.minion_machines
    #         if machine.status != constants.MINION_MACHINE_STATUS_AVAILABLE}

    #     if allocated_machine_statuses:
    #         raise exception.InvalidMinionPoolState(
    #             "Minion pool with ID '%s' has one or more machines which are "
    #             "in-use or otherwise unmodifiable: %s" % (
    #                 minion_pool.id,
    #                 allocated_machine_statuses))

    # @minion_manager_utils.minion_pool_synchronized_op
    # def deallocate_minion_pool_machines(self, ctxt, minion_pool_id, force=False):
    #     LOG.info("Attempting to deallocate Minion Pool '%s'.", minion_pool_id)
    #     minion_pool = db_api.get_minion_pool_lifecycle(
    #         ctxt, minion_pool_id, include_tasks_executions=False,
    #         include_machines=True)
    #     if minion_pool.status not in (
    #             constants.MINION_POOL_STATUS_ALLOCATED) and not force:
    #         raise exception.InvalidMinionPoolState(
    #             "Minion Pool '%s' cannot be deallocated as it is in '%s' "
    #             "state instead of the expected '%s'. Please use the "
    #             "force flag if you are certain you want to deallocate "
    #             "the minion pool's machines." % (
    #                 minion_pool_id, minion_pool.status,
    #                 constants.MINION_POOL_STATUS_ALLOCATED))

    #     if not force:
    #         self._check_all_pool_minion_machines_available(minion_pool)

    #     execution = models.TasksExecution()
    #     execution.id = str(uuid.uuid4())
    #     execution.action = minion_pool
    #     execution.status = constants.EXECUTION_STATUS_UNEXECUTED
    #     execution.type = (
    #         constants.EXECUTION_TYPE_MINION_POOL_DEALLOCATE_MINIONS)

    #     delete_minion_task_type = (
    #         constants.TASK_TYPE_DELETE_DESTINATION_MINION_MACHINE)
    #     if minion_pool.platform == constants.PROVIDER_PLATFORM_SOURCE:
    #         delete_minion_task_type = (
    #             constants.TASK_TYPE_DELETE_DESTINATION_MINION_MACHINE)

    #     for minion_machine in minion_pool.minion_machines:
    #         minion_machine_id = minion_machine.id
    #         minion_pool.info[minion_machine_id] = {
    #             "pool_environment_options": minion_pool.source_environment,
    #             "minion_provider_properties": (
    #                 minion_machine.provider_properties)}
    #         self._create_task(
    #             minion_machine_id, delete_minion_task_type,
    #             # NOTE: we set 'on_error=True' to allow for the completion of
    #             # already running deletion tasks to prevent partial deletes:
    #             execution, on_error=True)

    #     self._check_execution_tasks_sanity(execution, minion_pool.info)

    #     # update the action info for all of the pool's minions:
    #     for minion_machine in minion_pool.minion_machines:
    #         db_api.update_transfer_action_info_for_instance(
    #             ctxt, minion_pool.id, minion_machine.id,
    #             minion_pool.info[minion_machine.id])

    #     # add new execution to DB:
    #     db_api.add_minion_pool_lifecycle_execution(ctxt, execution)
    #     LOG.info(
    #         "Minion pool deallocation execution created: %s", execution.id)

    #     self._begin_tasks(ctxt, minion_pool, execution)
    #     db_api.set_minion_pool_lifecycle_status(
    #         ctxt, minion_pool.id, constants.MINION_POOL_STATUS_DEALLOCATING)

    #     return self._get_minion_pool_lifecycle_execution(
    #         ctxt, minion_pool_id, execution.id).to_dict()

    @minion_manager_utils.minion_pool_synchronized_op
    def get_minion_pool(self, ctxt, minion_pool_id):
        return self._get_minion_pool(
            ctxt, minion_pool_id, include_machines=True, include_events=True,
            include_progress_updates=True)

    @minion_manager_utils.minion_pool_synchronized_op
    def update_minion_pool(self, ctxt, minion_pool_id, updated_values):
        minion_pool = self._get_minion_pool(
            ctxt, minion_pool_id, include_machines=False)
        if minion_pool.status != constants.MINION_POOL_STATUS_DEALLOCATED:
            raise exception.InvalidMinionPoolState(
                "Minion Pool '%s' cannot be updated as it is in '%s' status "
                "instead of the expected '%s'. Please ensure the pool machines"
                "have been deallocated and the pool's supporting resources "
                "have been torn down before updating the pool." % (
                    minion_pool_id, minion_pool.status,
                    constants.MINION_POOL_STATUS_DEALLOCATED))
        LOG.info(
            "Attempting to update minion_pool '%s' with payload: %s",
            minion_pool_id, updated_values)
        db_api.update_minion_pool(ctxt, minion_pool_id, updated_values)
        LOG.info("Minion Pool '%s' successfully updated", minion_pool_id)
        return db_api.get_minion_pool(ctxt, minion_pool_id)

    @minion_manager_utils.minion_pool_synchronized_op
    def delete_minion_pool(self, ctxt, minion_pool_id):
        minion_pool = self._get_minion_pool(
            ctxt, minion_pool_id, include_machines=True)
        acceptable_deletion_statuses = [
            constants.MINION_POOL_STATUS_DEALLOCATED,
            constants.MINION_POOL_STATUS_ERROR]
        if minion_pool.status not in acceptable_deletion_statuses:
            raise exception.InvalidMinionPoolState(
                "Minion Pool '%s' cannot be deleted as it is in '%s' status "
                "instead of one of the expected '%s'. Please ensure the pool "
                "machines have been deallocated and the pool's supporting "
                "resources have been torn down before deleting the pool." % (
                    minion_pool_id, minion_pool.status,
                    acceptable_deletion_statuses))

        LOG.info("Deleting minion pool with ID '%s'" % minion_pool_id)
        db_api.delete_minion_pool(ctxt, minion_pool_id)

    # @minion_manager_utils.minion_pool_synchronized_op
    # def get_minion_pool_lifecycle_executions(
    #         self, ctxt, minion_pool_id, include_tasks=False):
    #     return db_api.get_minion_pool_lifecycle_executions(
    #         ctxt, minion_pool_id, include_tasks)

    # def _get_minion_pool_lifecycle_execution(
    #         self, ctxt, minion_pool_id, execution_id):
    #     execution = db_api.get_minion_pool_lifecycle_execution(
    #         ctxt, minion_pool_id, execution_id)
    #     if not execution:
    #         raise exception.NotFound(
    #             "Execution with ID '%s' for Minion Pool '%s' not found." % (
    #                 execution_id, minion_pool_id))
    #     return execution

    # @minion_pool_tasks_execution_synchronized
    # def get_minion_pool_lifecycle_execution(
    #         self, ctxt, minion_pool_id, execution_id):
    #     return self._get_minion_pool_lifecycle_execution(
    #         ctxt, minion_pool_id, execution_id).to_dict()

    # @minion_pool_tasks_execution_synchronized
    # def delete_minion_pool_lifecycle_execution(
    #         self, ctxt, minion_pool_id, execution_id):
    #     execution = self._get_minion_pool_lifecycle_execution(
    #         ctxt, minion_pool_id, execution_id)
    #     if execution.status in constants.ACTIVE_EXECUTION_STATUSES:
    #         raise exception.InvalidMigrationState(
    #             "Cannot delete execution '%s' for Minion pool '%s' as it is "
    #             "currently in '%s' state." % (
    #                 execution_id, minion_pool_id, execution.status))
    #     db_api.delete_minion_pool_lifecycle_execution(ctxt, execution_id)

    # @minion_pool_tasks_execution_synchronized
    # def cancel_minion_pool_lifecycle_execution(
    #         self, ctxt, minion_pool_id, execution_id, force):
    #     execution = self._get_minion_pool_lifecycle_execution(
    #         ctxt, minion_pool_id, execution_id)
    #     if execution.status not in constants.ACTIVE_EXECUTION_STATUSES:
    #         raise exception.InvalidMinionPoolState(
    #             "Minion pool '%s' has no running execution to cancel." % (
    #                 minion_pool_id))
    #     if execution.status == constants.EXECUTION_STATUS_CANCELLING and (
    #             not force):
    #         raise exception.InvalidMinionPoolState(
    #             "Execution for Minion Pool '%s' is already being cancelled. "
    #             "Please use the force option if you'd like to force-cancel "
    #             "it." % (minion_pool_id))
    #     self._cancel_tasks_execution(ctxt, execution, force=force)

    # @staticmethod
    # def _update_minion_pool_status_for_finished_execution(
    #         ctxt, execution, new_execution_status):
    #     # status map if execution is active:
    #     stat_map = {
    #         constants.EXECUTION_TYPE_MINION_POOL_ALLOCATE_MINIONS:
    #             constants.MINION_POOL_STATUS_ALLOCATING,
    #         constants.EXECUTION_TYPE_MINION_POOL_DEALLOCATE_MINIONS:
    #             constants.MINION_POOL_STATUS_DEALLOCATING,
    #         constants.EXECUTION_TYPE_MINION_POOL_SET_UP_SHARED_RESOURCES:
    #             constants.MINION_POOL_STATUS_INITIALIZING,
    #         constants.EXECUTION_TYPE_MINION_POOL_TEAR_DOWN_SHARED_RESOURCES:
    #             constants.MINION_POOL_STATUS_UNINITIALIZING}
    #     if new_execution_status == constants.EXECUTION_STATUS_COMPLETED:
    #         stat_map = {
    #             constants.EXECUTION_TYPE_MINION_POOL_ALLOCATE_MINIONS:
    #                 constants.MINION_POOL_STATUS_ALLOCATED,
    #             constants.EXECUTION_TYPE_MINION_POOL_DEALLOCATE_MINIONS:
    #                 constants.MINION_POOL_STATUS_DEALLOCATED,
    #             constants.EXECUTION_TYPE_MINION_POOL_SET_UP_SHARED_RESOURCES:
    #                 constants.MINION_POOL_STATUS_DEALLOCATED,
    #             constants.EXECUTION_TYPE_MINION_POOL_TEAR_DOWN_SHARED_RESOURCES:
    #                 constants.MINION_POOL_STATUS_UNINITIALIZED}
    #     elif new_execution_status in constants.FINALIZED_TASK_STATUSES:
    #         stat_map = {
    #             constants.EXECUTION_TYPE_MINION_POOL_ALLOCATE_MINIONS:
    #                 constants.MINION_POOL_STATUS_DEALLOCATED,
    #             constants.EXECUTION_TYPE_MINION_POOL_DEALLOCATE_MINIONS:
    #                 constants.MINION_POOL_STATUS_ALLOCATED,
    #             constants.EXECUTION_TYPE_MINION_POOL_SET_UP_SHARED_RESOURCES:
    #                 constants.MINION_POOL_STATUS_UNINITIALIZED,
    #             constants.EXECUTION_TYPE_MINION_POOL_TEAR_DOWN_SHARED_RESOURCES:
    #                 constants.MINION_POOL_STATUS_UNINITIALIZED}
    #     final_pool_status = stat_map.get(execution.type)
    #     if not final_pool_status:
    #         LOG.error(
    #             "Could not determine pool status following transition of "
    #             "execution '%s' (type '%s') to status '%s'. Presuming error "
    #             "has occured. Marking piil as error'd.",
    #             execution.id, execution.type, new_execution_status)
    #         final_pool_status = constants.MINION_POOL_STATUS_ERROR

    #     LOG.info(
    #         "Marking minion pool '%s' status as '%s' in the DB following the "
    #         "transition of execution '%s' (type '%s') to status '%s'.",
    #         execution.action_id, final_pool_status, execution.id,
    #         execution.type, new_execution_status)
    #     db_api.set_minion_pool_status(
    #         ctxt, execution.action_id, final_pool_status)

    # def deallocate_minion_machines_for_action(self, ctxt, action_id):
    #     if not isinstance(action, dict):
    #         raise exception.InvalidInput(
    #             "Action must be a dict, got '%s': %s" % (
    #                 type(action), action))
    #     required_action_properties = [
    #         'id', 'instances', 'origin_minion_pool_id',
    #         'destination_minion_pool_id',
    #         'instance_osmorphing_minion_pool_mappings']
    #     missing = [
    #         prop for prop in required_action_properties
    #         if prop not in action]
    #     if missing:
    #         raise exception.InvalidInput(
    #             "Missing the following required action properties for "
    #             "minion pool machine deallocation: %s. Got %s" % (
    #                 missing, action))

    #     minion_pool_ids = set()
    #     if action['origin_minion_pool_id']:
    #         minion_pool_ids.add(action['origin_minion_pool_id'])
    #     if action['destination_minion_pool_id']:
    #         minion_pool_ids.add(action['destination_minion_pool_id'])
    #     if action['instance_osmorphing_minion_pool_mappings']:
    #         minion_pool_ids = minion_pool_ids.union(set(
    #             action['instance_osmorphing_minion_pool_mappings'].values()))
    #     if None in minion_pool_ids:
    #         minion_pool_ids.remove(None)

    #     if not minion_pool_ids:
    #         LOG.debug(
    #             "No minion pools seem to have been used for action with "
    #             "base_id '%s'. Skipping minion machine deallocation.",
    #             action['id'])
    #     else:
    #         LOG.debug(
    #             "Attempting to deallocate all minion pool machine selections "
    #             "for action '%s'. Afferent pools are: %s",
    #             action['id'], minion_pool_ids)

    #         with contextlib.ExitStack() as stack:
    #             _ = [
    #                 stack.enter_context(
    #                     lockutils.lock(
    #                         constants.MINION_POOL_LOCK_NAME_FORMAT % pool_id,
    #                         external=True))
    #                 for pool_id in minion_pool_ids]

    #             minion_machines = db_api.get_minion_machines(
    #                 ctxt, allocated_action_id=action['id'])
    #             machine_ids = [m.id for m in minion_machines]
    #             if machine_ids:
    #                 LOG.info(
    #                     "Releasing the following minion machines for "
    #                     "action '%s': %s", action['base_id'], machine_ids)
    #                 db_api.set_minion_machines_allocation_statuses(
    #                     ctxt, machine_ids, None,
    #                     constants.MINION_MACHINE_STATUS_AVAILABLE)
    #             else:
    #                 LOG.debug(
    #                     "No minion machines were found to be associated "
    #                     "with action with base_id '%s'.", action['base_id'])

    # def _allocate_minion_machines_for_action(
    #         self, ctxt, action, include_transfer_minions=True,
    #         include_osmorphing_minions=True):
    #     """ Returns a dict of the form:
    #     {
    #         "instance_id": {
    #             "source_minion": <source minion properties>,
    #             "destination_minion": <target minion properties>,
    #             "osmorphing_minion": <osmorphing minion properties>
    #         }
    #     }
    #     """
    #     required_action_properties = [
    #         'id', 'instances', 'origin_minion_pool_id',
    #         'destination_minion_pool_id',
    #         'instance_osmorphing_minion_pool_mappings']
    #     self._check_keys_for_action_dict(
    #         action, required_action_properties,
    #         operation="minion machine selection")

    #     instance_machine_allocations = {
    #         instance: {} for instance in action['instances']}

    #     minion_pool_ids = set()
    #     if action['origin_minion_pool_id']:
    #         minion_pool_ids.add(action['origin_minion_pool_id'])
    #     if action['destination_minion_pool_id']:
    #         minion_pool_ids.add(action['destination_minion_pool_id'])
    #     if action['instance_osmorphing_minion_pool_mappings']:
    #         minion_pool_ids = minion_pool_ids.union(set(
    #             action['instance_osmorphing_minion_pool_mappings'].values()))
    #     if None in minion_pool_ids:
    #         minion_pool_ids.remove(None)

    #     if not minion_pool_ids:
    #         LOG.debug(
    #             "No minion pool settings found for action '%s'. "
    #             "Skipping minion machine allocations." % (
    #                 action['id']))
    #         return instance_machine_allocations

    #     LOG.debug(
    #         "All minion pool selections for action '%s': %s",
    #         action['id'], minion_pool_ids)

    #     def _select_machine(minion_pool, exclude=None):
    #         if not minion_pool.minion_machines:
    #             raise exception.InvalidMinionPoolSelection(
    #                 "Minion pool with ID '%s' has no machines defined." % (
    #                     minion_pool.id))
    #         selected_machine = None
    #         for machine in minion_pool.minion_machines:
    #             if exclude and machine.id in exclude:
    #                 LOG.debug(
    #                     "Excluding minion machine '%s' from search.",
    #                     machine.id)
    #                 continue
    #             if machine.status != constants.MINION_MACHINE_STATUS_AVAILABLE:
    #                 LOG.debug(
    #                     "Minion machine with ID '%s' is in status '%s' "
    #                     "instead of '%s'. Skipping.", machine.id,
    #                     machine.status,
    #                     constants.MINION_MACHINE_STATUS_AVAILABLE)
    #                 continue
    #             selected_machine = machine
    #             break
    #         if not selected_machine:
    #             raise exception.InvalidMinionPoolSelection(
    #                 "There are no more available minion machines within minion"
    #                 " pool with ID '%s' (excluding the following ones already "
    #                 "planned for this transfer: %s). Please ensure that the "
    #                 "minion pool has enough minion machines allocated and "
    #                 "available (i.e. not being used for other operations) "
    #                 "to satisfy the number of VMs required by the Migration or"
    #                 " Replica." % (
    #                     minion_pool.id, exclude))
    #         return selected_machine

    #     osmorphing_pool_map = (
    #         action['instance_osmorphing_minion_pool_mappings'])
    #     with contextlib.ExitStack() as stack:
    #         _ = [
    #             stack.enter_context(
    #                 minion_manager_utils.get_minion_pool_lock(
    #                     pool_id, external=True))
    #             for pool_id in minion_pool_ids]

    #         minion_pools = db_api.get_minion_pools(
    #             ctxt, include_machines=True, to_dict=False)
    #         minion_pool_id_mappings = {
    #             pool.id: pool for pool in minion_pools
    #             if pool.id in minion_pool_ids}

    #         missing_pools = [
    #             pool_id for pool_id in minion_pool_ids
    #             if pool_id not in minion_pool_id_mappings]
    #         if missing_pools:
    #             raise exception.InvalidMinionPoolSelection(
    #                 "The following minion pools could not be found: %s" % (
    #                     missing_pools))

    #         unallocated_pools = {
    #             pool_id: pool.status
    #             for (pool_id, pool) in minion_pool_id_mappings.items()
    #             if pool.status != constants.MINION_POOL_STATUS_ALLOCATED}
    #         if unallocated_pools:
    #             raise exception.InvalidMinionPoolSelection(
    #                 "The following minion pools have not had their machines "
    #                 "allocated and thus cannot be used: %s" % (
    #                     unallocated_pools))

    #         allocated_source_machine_ids = set()
    #         allocated_target_machine_ids = set()
    #         allocated_osmorphing_machine_ids = set()
    #         for instance in action['instances']:

    #             if include_transfer_minions:
    #                 if action['origin_minion_pool_id']:
    #                     origin_pool = minion_pool_id_mappings[
    #                         action['origin_minion_pool_id']]
    #                     machine = _select_machine(
    #                         origin_pool, exclude=allocated_source_machine_ids)
    #                     allocated_source_machine_ids.add(machine.id)
    #                     instance_machine_allocations[
    #                         instance]['source_minion'] = machine
    #                     LOG.debug(
    #                         "Selected minion machine '%s' for source-side "
    #                         "syncing of instance '%s' as part of transfer "
    #                         "action '%s'.", machine.id, instance, action['id'])

    #                 if action['destination_minion_pool_id']:
    #                     dest_pool = minion_pool_id_mappings[
    #                         action['destination_minion_pool_id']]
    #                     machine = _select_machine(
    #                         dest_pool, exclude=allocated_target_machine_ids)
    #                     allocated_target_machine_ids.add(machine.id)
    #                     instance_machine_allocations[
    #                         instance]['destination_minion'] = machine
    #                     LOG.debug(
    #                         "Selected minion machine '%s' for target-side "
    #                         "syncing of instance '%s' as part of transfer "
    #                         "action '%s'.", machine.id, instance, action['id'])

    #             if include_osmorphing_minions:
    #                 if instance not in osmorphing_pool_map:
    #                     LOG.debug(
    #                         "Instance '%s' is not listed in the OSMorphing "
    #                         "minion pool mappings for action '%s'." % (
    #                             instance, action['id']))
    #                 elif osmorphing_pool_map[instance] is None:
    #                     LOG.debug(
    #                         "OSMorphing pool ID for instance '%s' is "
    #                         "None in action '%s'. Ignoring." % (
    #                             instance, action['id']))
    #                 else:
    #                     osmorphing_pool_id = osmorphing_pool_map[instance]
    #                     # if the selected target and OSMorphing pools
    #                     # are the same, reuse the same worker:
    #                     ima = instance_machine_allocations[instance]
    #                     if osmorphing_pool_id == (
    #                             action['destination_minion_pool_id']) and (
    #                                 'destination_minion' in ima):
    #                         allocated_target_machine = ima[
    #                             'destination_minion']
    #                         LOG.debug(
    #                             "Reusing disk sync minion '%s' for the "
    #                             "OSMorphing of instance '%s' as part of "
    #                             "transfer action '%s'",
    #                             allocated_target_machine.id, instance,
    #                             action['id'])
    #                         instance_machine_allocations[
    #                             instance]['osmorphing_minion'] = (
    #                                 allocated_target_machine)
    #                     # else, allocate a new minion from the selected pool:
    #                     else:
    #                         osmorphing_pool = minion_pool_id_mappings[
    #                             osmorphing_pool_id]
    #                         machine = _select_machine(
    #                             osmorphing_pool,
    #                             exclude=allocated_osmorphing_machine_ids)
    #                         allocated_osmorphing_machine_ids.add(machine.id)
    #                         instance_machine_allocations[
    #                             instance]['osmorphing_minion'] = machine
    #                         LOG.debug(
    #                             "Selected minion machine '%s' for OSMorphing "
    #                             " of instance '%s' as part of transfer "
    #                             "action '%s'.",
    #                             machine.id, instance, action['id'])

    #         # mark the selected machines as allocated:
    #         all_machine_ids = set(itertools.chain(
    #             allocated_source_machine_ids,
    #             allocated_target_machine_ids,
    #             allocated_osmorphing_machine_ids))
    #         db_api.set_minion_machines_allocation_statuses(
    #             ctxt, all_machine_ids, action['id'],
    #             constants.MINION_MACHINE_STATUS_IN_USE,
    #             refresh_allocation_time=True)

    #     # filter out redundancies:
    #     instance_machine_allocations = {
    #         instance: allocations
    #         for (instance, allocations) in instance_machine_allocations.items()
    #         if allocations}

    #     LOG.debug(
    #         "Allocated the following minion machines for action '%s': %s",
    #         action['id'], {
    #             instance: {
    #                 typ: machine.id
    #                 for (typ, machine) in allocation.items()}
    #             for (instance, allocation) in instance_machine_allocations.items()})
    #     return instance_machine_allocations
