import uuid

import json

from oslo_log import log as logging

from coriolis import constants
from coriolis.db import api as db_api
from coriolis.db.sqlalchemy import models
from coriolis.worker.rpc import client as rpc_worker_client

VERSION = "1.0"

LOG = logging.getLogger(__name__)


class ConductorServerEndpoint(object):
    def __init__(self):
        self._rpc_worker_client = rpc_worker_client.WorkerClient()

    def get_migrations(self, ctxt):
        return db_api.get_migrations(ctxt)

    def get_migration(self, ctxt, migration_id):
        return db_api.get_migration(ctxt, migration_id)

    def migrate_instances(self, ctxt, origin, destination, instances):
        migration = models.Migration()
        migration.user_id = "todo"
        migration.status = constants.MIGRATION_STATUS_STARTED
        migration.origin = json.dumps(origin)
        migration.destination = json.dumps(destination)

        for instance in instances:
            task = models.Task()
            task.id = str(uuid.uuid4())
            task.migration = migration
            task.instance = instance
            task.status = constants.TASK_STATUS_STARTED
            task.task_type = constants.TASK_TYPE_EXPORT

        db_api.add(ctxt, migration)
        LOG.info("Migration created: %s", migration.id)

        for task in migration.tasks:
            self._rpc_worker_client.begin_export_instance(
                ctxt, task.id, origin, instance)

    def stop_instances_migration(self, ctxt, migration_id):
        migration = db_api.get_migration(ctxt, migration_id)
        for task in migration.tasks:
            if task.status == constants.TASK_STATUS_STARTED:
                self._rpc_worker_client.stop_task(
                    ctxt, task.host, task.process_id)

    def set_task_host(self, ctxt, task_id, host, process_id):
        db_api.set_task_host(ctxt, task_id, host, process_id)

    def export_completed(self, ctxt, task_id, export_info):
        db_api.update_task_status(
            ctxt, task_id, constants.TASK_STATUS_COMPLETE)
        op_export = db_api.get_task(ctxt, task_id)

        op_import = models.Task()
        op_import.id = str(uuid.uuid4())
        op_import.migration = op_export.migration
        op_import.instance = op_export.instance
        op_import.status = constants.TASK_STATUS_STARTED
        op_import.task_type = constants.TASK_TYPE_IMPORT

        db_api.add(ctxt, op_import)

        self._rpc_worker_client.begin_import_instance(
            ctxt, op_export.host, op_import.id,
            json.loads(op_import.migration.destination),
            op_import.instance,
            export_info)

    def import_completed(self, ctxt, task_id):
        db_api.update_task_status(
            ctxt, task_id, constants.TASK_STATUS_COMPLETE)

    def set_task_error(self, ctxt, task_id, exception_details):
        db_api.update_task_status(
            ctxt, task_id, constants.TASK_STATUS_ERROR,
            exception_details)
        # TODO: set migration in error state and canel other tasks
