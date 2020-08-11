# Copyright 2020 Cloudbase Solutions Srl
# All Rights Reserved.

from oslo_log import log as logging

from coriolis import constants
from coriolis.scheduler.filters import base


LOG = logging.getLogger(__name__)


class RegionsFilter(base.BaseServiceFilter):

    def __init__(self, regions):
        self._regions = regions

    def __repr__(self):
        return "<%s(regions=%s)>" % (
            self.__class__.__name__, self._regions)

    def rate_service(self, service):
        service_regions = [
            mapping["region_id"] for mapping in service.mapped_regions]
        missing_regions = [
            region
            for region in self._regions
            if region not in service_regions]

        if missing_regions:
            LOG.debug(
                "The following required regions are missing from service "
                "with ID '%s': %s", service.id, missing_regions)
            return 0

        return 100


class TopicFilter(base.BaseServiceFilter):

    def __init__(self, topic):
        self._topic = topic

    def __repr__(self):
        return "<%s(topic=%s)>" % (
            self.__class__.__name__, self._topic)

    def rate_service(self, service):
        if service.topic == self._topic:
            return 100
        return 0


class EnabledFilter(base.BaseServiceFilter):

    def __init__(self, enabled=True):
        self._enabled = enabled

    def __repr__(self):
        return "<%s(enabled=%s)>" % (
            self.__class__.__name__, self._enabled)

    def rate_service(self, service):
        if service.enabled == self._enabled:
            return 100
        return 0


class ProviderTypesFilter(base.BaseServiceFilter):

    def __init__(self, provider_requirements):
        """ Filters based on requested provider capabilities.
        :param provider_requirements: dict of the form {
            "<platform_type>": [constants.PROVIDER_TYPE_*, ...]}
        """
        self._provider_requirements = provider_requirements

    def __repr__(self):
        return "<%s(provider_requirements=%s)>" % (
            self.__class__.__name__, self._provider_requirements)

    def rate_service(self, service):
        for platform_type in self._provider_requirements:
            if platform_type not in service.providers:
                LOG.debug(
                    "Service with ID '%s' does not have a provider for platform "
                    "type '%s'", service.id, platform_type)
                return 0

            available_types = service.providers[
                platform_type].get('types', [])
            missing_types = [
                typ for typ in self._provider_requirements[platform_type]
                if typ not in available_types]
            if missing_types:
                LOG.debug(
                    "Service with ID '%s' is missing the following required "
                    "provider types for platform '%s': %s",
                    service.id, platform_type, missing_types)
                return 0

        return 100
