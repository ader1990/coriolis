# Copyright 2016 Cloudbase Solutions Srl
# All Rights Reserved.

import uuid

from coriolis.osmorphing.osdetect import oracle as oracle_detect
from coriolis.osmorphing import redhat


ORACLE_DISTRO_IDENTIFIER = oracle_detect.ORACLE_DISTRO_IDENTIFIER


class BaseOracleMorphingTools(redhat.BaseRedHatMorphingTools):

    @classmethod
    def check_os_supported(cls, detected_os_info):
        if detected_os_info['distribution_name'] != (
                ORACLE_DISTRO_IDENTIFIER):
            return False
        return cls._version_supported_util(
            detected_os_info['release_version'], minimum=6)

    def _get_oracle_repos(self):
        repos = []
        major_version = int(self._version.split(".")[0])
        uekr_version = int(major_version) - 2
        if major_version < 8:
            repo_file_path = (
                '/etc/yum.repos.d/%s.repo' % str(uuid.uuid4()))
            self._exec_cmd_chroot(
                "curl -L http://public-yum.oracle.com/public-yum-ol%s.repo "
                "-o %s" % (major_version, repo_file_path))
            # During OSMorphing, we temporarily enable needed package repos,
            # so we make sure we disable all downloaded repos here.
            self._exec_cmd_chroot(
                'sed -i "s/^enabled=1$/enabled=0/g" %s' % repo_file_path)

            repos_to_enable = ["ol%s_software_collections" % major_version,
                               "ol%s_addons" % major_version,
                               "ol%s_UEKR" % major_version,
                               "ol%s_latest" % major_version]
            repos = self._find_yum_repos(repos_to_enable)
        else:
            self._yum_install(
                ['oraclelinux-release-el%s' % major_version],
                self._find_yum_repos(['ol%s_baseos_latest' % major_version]))
            repos_to_enable = ["ol%s_baseos_latest" % major_version,
                               "ol%s_appstream" % major_version,
                               "ol%d_addons" % major_version,
                               "ol%s_UEKR%s" % (major_version, uekr_version)]
            repos = self._find_yum_repos(repos_to_enable)

        return repos
