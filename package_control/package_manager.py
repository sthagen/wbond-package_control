import datetime
import json
# To prevent import errors in thread with datetime
import locale  # noqa
import os
import re
import shutil
import tempfile
import time
import zipfile

from io import BytesIO
from threading import RLock
from urllib.parse import urlencode, urlparse

import sublime

from . import __version__
from . import library, pep440, sys_path, text
from .cache import clear_cache, set_cache, get_cache, merge_cache_under_settings, set_cache_under_settings
from .clear_directory import clear_directory, delete_directory
from .clients.client_exception import ClientException
from .console_write import console_write
from .download_manager import http_get
from .downloaders.background_downloader import BackgroundDownloader
from .downloaders.downloader_exception import DownloaderException
from .package_io import (
    create_empty_file,
    get_installed_package_path,
    get_package_dir,
    get_package_cache_dir,
    get_package_module_cache_dir,
    list_sublime_package_dirs,
    list_sublime_package_files,
    read_package_file,
    regular_file_exists,
    zip_file_exists,
)
from .package_version import PackageVersion, version_sort
from .providers import CHANNEL_PROVIDERS, REPOSITORY_PROVIDERS
from .providers.channel_provider import UncachedChannelRepositoryError
from .providers.provider_exception import ProviderException
from .selectors import is_compatible_version, is_compatible_platform, get_compatible_platform
from .settings import load_list_setting, pc_settings_filename
from .upgraders.git_upgrader import GitUpgrader
from .upgraders.hg_upgrader import HgUpgrader


DEFAULT_CHANNEL = 'https://packagecontrol.io/channel_v3.json'
OLD_DEFAULT_CHANNELS = set([
    'https://packagecontrol.io/channel.json',
    'https://sublime.wbond.net/channel.json',
    'https://sublime.wbond.net/repositories.json'
])


class PackageManager:

    """
    Allows downloading, creating, installing, upgrading, and deleting packages

    Delegates metadata retrieval to the CHANNEL_PROVIDERS classes.
    Uses VcsUpgrader-based classes for handling git and hg repositories in the
    Packages folder. Downloader classes are utilized to fetch contents of URLs.

    Also handles displaying package messaging, and sending usage information to
    the usage server.
    """

    lock = RLock()

    def __init__(self):
        """
        Constructs a new instance.
        """

        self._available_packages = None
        self._available_libraries = None

        self.settings = {}
        settings = sublime.load_settings(pc_settings_filename())
        setting_names = [
            'auto_migrate',
            'auto_upgrade',
            'auto_upgrade_frequency',
            'auto_upgrade_ignore',
            'cache_length',
            'channels',
            'debug',
            'dirs_to_ignore',
            'downloader_precedence',
            'files_to_ignore',
            'files_to_include',
            'git_binary',
            'git_update_command',
            'hg_binary',
            'hg_update_command',
            'http_basic_auth',
            'http_cache',
            'http_cache_length',
            'http_proxy',
            'https_proxy',
            'ignore_vcs_packages',
            'install_missing',
            'install_prereleases',
            'max_backup_age',
            'package_destination',
            'package_name_map',
            'package_profiles',
            'proxy_password',
            'proxy_username',
            'remove_orphaned',
            'renamed_packages',
            'repositories',
            'submit_url',
            'submit_usage',
            'timeout',
            'user_agent'
        ]
        for setting in setting_names:
            value = settings.get(setting)
            if value is not None:
                self.settings[setting] = value

        # https_proxy will inherit from http_proxy unless it is set to a
        # string value or false
        no_https_proxy = self.settings.get('https_proxy') in ["", None]
        if no_https_proxy and self.settings.get('http_proxy'):
            self.settings['https_proxy'] = self.settings.get('http_proxy')
        if self.settings.get('https_proxy') is False:
            self.settings['https_proxy'] = ''

        self.settings['max_releases'] = 1  # fetch latest release only, from code hosters

        # We cache these to prevent IPC calls between plugin_host and the main
        # Sublime Text executable
        self.settings['platform'] = sublime.platform()
        self.settings['arch'] = sublime.arch()
        self.settings['version'] = int(sublime.version())

        # Use the cache to see if settings have changed since the last
        # time the package manager was created, and clearing any cached
        # values if they have.
        previous_settings = get_cache('filtered_settings', {})

        # Reduce the settings down to exclude channel info since that will
        # make the settings always different
        filtered_settings = self.settings.copy()
        for key in ['repositories', 'channels', 'package_name_map', 'cache']:
            if key in filtered_settings:
                del filtered_settings[key]

        if filtered_settings != previous_settings and previous_settings != {}:
            console_write(
                '''
                Settings change detected, clearing cache
                '''
            )
            clear_cache()
        set_cache('filtered_settings', filtered_settings)

    def get_mapped_name(self, package_name):
        """:return: The name of the package after passing through mapping rules"""

        return self.settings.get('package_name_map', {}).get(package_name, package_name)

    def get_metadata(self, package_name):
        """
        Returns the package metadata for an installed package

        :param package_name:
            The name of the package

        :return:
            A dict with the keys:
                version
                url
                description
            or an empty dict on error
        """

        metadata_json = read_package_file(package_name, 'package-metadata.json')
        if metadata_json:
            try:
                return json.loads(metadata_json)
            except (ValueError):
                console_write(
                    '''
                    Failed to parse package metadata for "%s"
                    ''',
                    package_name
                )

        return {}

    def get_libraries(self, package_name):
        """
        Returns a list of libraries for the specified package on the
        current machine

        :param package_name:
            The name of the package

        :return:
            A set of library.Library() objects
        """

        python_version = self.get_python_version(package_name)

        names = None

        lib_info_json = read_package_file(package_name, 'dependencies.json')
        if lib_info_json:
            try:
                names = self.select_libraries(json.loads(lib_info_json))
            except (ValueError):
                console_write(
                    '''
                    Failed to parse the dependencies.json for "%s"
                    ''',
                    package_name
                )

        if names is None:
            metadata = self.get_metadata(package_name)
            # "dependencies" key is for backwards compatibility
            names = metadata.get('libraries', metadata.get('dependencies', []))

        if not names:
            return set()

        return set(library.Library(name, python_version) for name in names)

    def get_python_version(self, package_name):
        """
        Returns the version of python a package runs under

        :param package_name:
            The name of the package

        :return:
            A unicode string of "3.3" or "3.8"
        """

        python_version = read_package_file(package_name, ".python-version")
        if python_version:
            python_version = python_version.strip()
            if python_version in sys_path.lib_paths():
                return python_version

        return "3.3"

    def get_version(self, package_name):
        """
        Determines the current version for a package

        :param package_name:
            The package name
        """

        version = self.get_metadata(package_name).get('version')

        if version:
            return version

        upgrader = self.instantiate_upgrader(package_name)
        if upgrader:
            version = upgrader.latest_commit()
            if version:
                return '%s commit %s' % (upgrader.cli_name, version)

        return 'unknown version'

    def is_compatible(self, package_name):
        """
        Detects if a package is compatible with the current Sublime Text install

        :param package_name:
            A package's name string to check for compatibility

        :return:
            If the package is compatible
        """

        metadata = self.get_metadata(package_name)
        if not metadata:
            # unmanaged or unable to parse meta data
            # can't say something about compatibility, asume the best
            return True

        sublime_text = metadata.get('sublime_text')
        platforms = metadata.get('platforms', [])

        # This indicates the metadata is old, so we assume a match
        if not sublime_text and not platforms:
            return True

        return is_compatible_platform(platforms) and is_compatible_version(sublime_text)

    def _is_git_package(self, package_name):
        """
        :param package_name:
            The package name

        :return:
            If the package is installed via git
        """

        git_dir = os.path.join(get_package_dir(package_name), '.git')
        return os.path.isdir(git_dir) or os.path.isfile(git_dir)

    def _is_hg_package(self, package_name):
        """
        :param package_name:
            The package name

        :return:
            If the package is installed via hg
        """

        hg_dir = os.path.join(get_package_dir(package_name), '.hg')
        return os.path.isdir(hg_dir)

    def is_vcs_package(self, package_name):
        """
        If the package is installed via git or hg

        :param package_name:
            The package to check

        :return:
            bool
        """

        return self._is_git_package(package_name) or self._is_hg_package(package_name)

    def instantiate_upgrader(self, package_name):
        """
        Creates an HgUpgrader or GitUpgrader object to run operations on a VCS-
        based package

        :param package_name:
            The name of the package

        :return:
            GitUpgrader, HgUpgrader or None
        """

        if self._is_git_package(package_name):
            return GitUpgrader(
                self.settings['git_binary'],
                self.settings['git_update_command'],
                get_package_dir(package_name),
                self.settings['cache_length'],
                self.settings['debug']
            )

        if self._is_hg_package(package_name):
            return HgUpgrader(
                self.settings['hg_binary'],
                self.settings['hg_update_command'],
                get_package_dir(package_name),
                self.settings['cache_length'],
                self.settings['debug']
            )

        return None

    def select_releases(self, package_name, releases):
        """
        Returns all releases in the list of releases that are compatible with
        the current platform and version of Sublime Text

        :param package_name:
            The name of the package

        :param releases:
            A list of release dicts

        :return:
            A list of release dicts
        """

        install_prereleases = self.settings.get('install_prereleases')
        allow_prereleases = (
            install_prereleases is True
            or isinstance(install_prereleases, list) and package_name in install_prereleases
        )

        return [
            release for release in releases
            if is_compatible_platform(release['platforms'])
            and is_compatible_version(release['sublime_text'])
            and (allow_prereleases or not PackageVersion(release['version']).prerelease)
        ]

    def select_libraries(self, library_info):
        """
        Takes the a dict from a dependencies.json file and returns the
        library names that are applicable to the current machine

        :param library_info:
            A dict from a dependencies.json file

        :return:
            A list of library names
        """

        platforms = list(library_info.keys())
        platform_selector = get_compatible_platform(platforms)
        if platform_selector:
            platform_library = library_info[platform_selector]

            # Sorting reverse will give us >, < then *
            versions = sorted(platform_library.keys(), reverse=True)
            for version_selector in versions:
                if is_compatible_version(version_selector):
                    return platform_library[version_selector]

        # If there were no matches in the info, but there also weren't any
        # errors, then it just means there are not libraries for this machine
        return []

    def list_repositories(self):
        """
        Returns a master list of all repositories pulled from all sources

        These repositories come from the channels specified in the
        "channels" setting, plus any repositories listed in the
        "repositories" setting.

        :return:
            A list of all available repositories
        """

        cache_ttl = self.settings.get('cache_length', 300)

        repositories = self.settings.get('repositories', [])
        channels = self.settings.get('channels', [])

        # Update any old default channel URLs users have in their config
        found_default = False
        for channel in channels:
            channel = channel.strip()

            if re.match(r'https?://([^.]+\.)*package-control\.io', channel):
                console_write('Removed malicious channel %s' % channel)
                continue

            if channel in OLD_DEFAULT_CHANNELS:
                if found_default:
                    continue
                found_default = True
                channel = DEFAULT_CHANNEL

            # Caches various info from channels for performance
            cache_key = channel + '.repositories'
            channel_repositories = get_cache(cache_key)

            merge_cache_under_settings(self, 'renamed_packages', channel)
            merge_cache_under_settings(self, 'unavailable_packages', channel, list_=True)
            merge_cache_under_settings(self, 'unavailable_libraries', channel, list_=True)

            # If any of the info was not retrieved from the cache, we need to
            # grab the channel to get it
            if channel_repositories is None:

                for provider_class in CHANNEL_PROVIDERS:
                    if provider_class.match_url(channel):
                        provider = provider_class(channel, self.settings)
                        break
                else:
                    continue

                try:
                    channel_repositories = provider.get_repositories()
                    set_cache(cache_key, channel_repositories, cache_ttl)

                    unavailable_packages = []
                    unavailable_libraries = []

                    for repo in channel_repositories:

                        try:
                            filtered_packages = {}
                            for name, info in provider.get_packages(repo):
                                info['releases'] = self.select_releases(name, info['releases'])
                                if info['releases']:
                                    filtered_packages[name] = info
                                else:
                                    unavailable_packages.append(name)

                            packages_cache_key = repo + '.packages'
                            set_cache(packages_cache_key, filtered_packages, cache_ttl)

                        except UncachedChannelRepositoryError:
                            pass

                        try:
                            filtered_libraries = {}
                            for name, info in provider.get_libraries(repo):
                                info['releases'] = self.select_releases(name, info['releases'])
                                if info['releases']:
                                    filtered_libraries[name] = info
                                else:
                                    unavailable_libraries.append(name)

                            libraries_cache_key = repo + '.libraries'
                            set_cache(libraries_cache_key, filtered_libraries, cache_ttl)

                        except UncachedChannelRepositoryError:
                            pass

                    renamed_packages = provider.get_renamed_packages()
                    set_cache_under_settings(self, 'renamed_packages', channel, renamed_packages, cache_ttl)

                    set_cache_under_settings(
                        self,
                        'unavailable_packages',
                        channel,
                        unavailable_packages,
                        cache_ttl,
                        list_=True
                    )
                    set_cache_under_settings(
                        self,
                        'unavailable_libraries',
                        channel,
                        unavailable_libraries,
                        cache_ttl,
                        list_=True
                    )

                except (DownloaderException, ClientException, ProviderException) as e:
                    console_write(e)
                    continue

            repositories.extend(channel_repositories)

        return [repo.strip() for repo in repositories]

    def fetch_available(self):
        """
        Fetch available packages and libraries from available sources.

        use results from:

        1. in-memory cache (if not out-dated)
        2. http cache (if remote returns 304)
        3. download info from remote and store in caches

        :return:
            Nothing
        """

        if self.settings.get('debug'):
            console_write(
                '''
                Fetching list of available packages and libraries
                  Platform: %s-%s
                  Sublime Text Version: %s
                  Package Control Version: %s
                ''',
                (
                    self.settings['platform'],
                    self.settings['arch'],
                    self.settings['version'],
                    __version__
                )
            )

        cache_ttl = self.settings.get('cache_length', 300)
        repositories = self.list_repositories()
        packages = {}
        libraries = {}
        bg_downloaders = {}
        active = []
        repos_to_download = []
        name_map = self.settings.get('package_name_map', {})

        # Repositories are run in reverse order so that the ones first
        # on the list will overwrite those last on the list
        for repo in reversed(repositories):
            if re.match(r'https?://([^.]+\.)*package-control\.io', repo):
                console_write('Removed malicious repository %s' % repo)
                continue

            cache_key = repo + '.packages'
            repository_packages = get_cache(cache_key)

            if repository_packages:
                packages.update(repository_packages)

            cache_key = repo + '.libraries'
            repository_libraries = get_cache(cache_key)

            if repository_libraries:
                libraries.update(repository_libraries)

            if repository_packages is None and repository_libraries is None:
                domain = urlparse(repo).hostname
                if domain not in bg_downloaders:
                    bg_downloaders[domain] = BackgroundDownloader(
                        self.settings, REPOSITORY_PROVIDERS)
                bg_downloaders[domain].add_url(repo)
                repos_to_download.append(repo)

        for bg_downloader in bg_downloaders.values():
            bg_downloader.start()
            active.append(bg_downloader)

        # Wait for all of the downloaders to finish
        while active:
            bg_downloader = active.pop()
            bg_downloader.join()

        # Grabs the results and stuff it all in the cache
        for repo in repos_to_download:
            domain = urlparse(repo).hostname
            bg_downloader = bg_downloaders[domain]
            provider = bg_downloader.get_provider(repo)
            if not provider:
                continue

            repository_packages = {}
            unavailable_packages = []
            for name, info in provider.get_packages():
                name = name_map.get(name, name)
                info['name'] = name
                info['releases'] = self.select_releases(name, info['releases'])
                if info['releases']:
                    repository_packages[name] = info
                else:
                    unavailable_packages.append(name)

            repository_libraries = {}
            unavailable_libraries = []
            for name, info in provider.get_libraries():
                info['releases'] = self.select_releases(name, info['releases'])
                if info['releases']:
                    repository_libraries[name] = info
                else:
                    unavailable_libraries.append(name)

            # Display errors we encountered while fetching package info
            for _, exception in provider.get_failed_sources():
                console_write(exception)
            for _, exception in provider.get_broken_packages():
                console_write(exception)
            for _, exception in provider.get_broken_libraries():
                console_write(exception)

            cache_key = repo + '.packages'
            set_cache(cache_key, repository_packages, cache_ttl)
            packages.update(repository_packages)

            cache_key = repo + '.libraries'
            set_cache(cache_key, repository_libraries, cache_ttl)
            libraries.update(repository_libraries)

            renamed_packages = provider.get_renamed_packages()
            set_cache_under_settings(self, 'renamed_packages', repo, renamed_packages, cache_ttl)

            set_cache_under_settings(
                self,
                'unavailable_packages',
                repo,
                unavailable_packages,
                cache_ttl,
                list_=True
            )
            set_cache_under_settings(
                self,
                'unavailable_libraries',
                repo,
                unavailable_libraries,
                cache_ttl,
                list_=True
            )

        self._available_packages = packages
        self._available_libraries = libraries

    def list_available_libraries(self, python_version):
        """
        Returns a master list of every available library from all sources that
        are compatible with the version of Python specified

        :param python_version:
            A unicode string of "3.3" or "3.8"

        :return:
            A dict in the format:
            {
                'Library Name': {
                    # library details - see example-repository.json for format
                },
                ...
            }
        """

        if self._available_libraries is None:
            self.fetch_available()

        filtered_libraries = {}
        for name, info in self._available_libraries.items():
            filtered_releases = [
                release for release in info["releases"]
                if python_version in release["python_versions"]
            ]
            if filtered_releases:
                filtered_libraries[name] = info

        return filtered_libraries

    def list_available_packages(self):
        """
        Returns a master list of every available package from all sources

        :return:
            A dict in the format:
            {
                'Package Name': {
                    # Package details - see example-repository.json for format
                },
                ...
            }
        """

        if self._available_packages is None:
            self.fetch_available()

        return self._available_packages or {}

    def list_libraries(self):
        """
        :return:
            A list of library.Library() objects for all installed libraries
        """

        return library.list_all()

    def list_packages(self, unpacked_only=False):
        """
        List installed packages on the machine

        :param unpacked_only:
            Only list packages that are not inside of .sublime-package files

        :return:
            A list of all installed, non-default, non-library, package names
        """

        packages = set(list_sublime_package_dirs(sys_path.packages_path()))
        if unpacked_only is False:
            packages |= set(list_sublime_package_files(sys_path.installed_packages_path()))
        packages -= set(list_sublime_package_files(sys_path.default_packages_path()))
        packages -= {'Binary', 'Default', 'Text', 'User'}
        return sorted(packages, key=lambda s: s.lower())

    def list_default_packages(self):
        """
        Lists all builtin packages shipped with ST

        :return:
            A list of default package names
        """

        packages = set(list_sublime_package_files(sys_path.default_packages_path()))
        packages -= {'Binary', 'Default', 'Text', 'User'}
        return sorted(packages, key=lambda s: s.lower())

    def list_all_packages(self):
        """
        Lists all packages on the machine

        :return:
            A list of all package names, including default packages
        """

        packages = set(list_sublime_package_dirs(sys_path.packages_path()))
        packages |= set(list_sublime_package_files(sys_path.installed_packages_path()))
        packages |= set(list_sublime_package_files(sys_path.default_packages_path()))
        packages -= {'Binary', 'Default', 'Text', 'User'}
        return sorted(packages, key=lambda s: s.lower())

    def installed_packages(self):
        """
        Return a set of installed package names from registy.

        :returns:
            A set of ``installed_packages``.
        """

        with PackageManager.lock:
            settings = sublime.load_settings(pc_settings_filename())
            return load_list_setting(settings, 'installed_packages')

    def update_installed_packages(self, add=None, remove=None, persist=True):
        """
        Add and/or remove packages to installed package registry.

        :param add:
            A list/tuple/set of or a unicode string with the packages
            to add to the list of installed packages.

        :param remove:
            A list/tuple/set of or a unicode string with the packages
            to remove from the list of installed packages.

        :param persist:
            Save changed settings to disk.

        :returns:
            A set of ``installed_packages``.
        """

        with PackageManager.lock:
            file_name = pc_settings_filename()
            settings = sublime.load_settings(file_name)
            names_at_start = load_list_setting(settings, 'installed_packages')
            names = names_at_start.copy()

            if add:
                if isinstance(add, str):
                    add = {add}
                elif isinstance(add, (list, tuple)):
                    add = set(add)
                names |= add

            if remove:
                if isinstance(remove, str):
                    remove = {remove}
                elif isinstance(remove, (list, tuple)):
                    remove = set(remove)
                names -= remove

            if names != names_at_start:
                settings.set('installed_packages', sorted(names, key=lambda s: s.lower()))
                if persist:
                    sublime.save_settings(file_name)

    def find_required_libraries(self, ignore_package=None):
        """
        Find all of the libraries required by the installed packages,
        ignoring the specified package.

        :param ignore_package:
            The package to ignore when enumerating libraries

        :return:
            A set of library.Library() objects for the libraries required by
            the installed packages
        """

        output = set()
        for package in self.list_packages():
            if package == ignore_package:
                continue
            output |= self.get_libraries(package)
        return output

    def find_missing_libraries(self, ignore_package=None, required_libraries=None):
        """
        Find missing libraries.

        :param ignore_package:
            The package to ignore when enumerating libraries

        :return:
            A set of library.Library() objects for missing libraries
        """

        installed_libraries = self.list_libraries()
        if required_libraries is None:
            required_libraries = self.find_required_libraries(ignore_package)
        return required_libraries - installed_libraries

    def find_orphaned_libraries(self, ignore_package=None, required_libraries=None):
        """
        Find orphaned libraries.

        :param ignore_package:
            The package to ignore when enumerating libraries

        :return:
            A set of library.Library() objects for no longer needed libraries
        """

        installed_libraries = self.list_libraries()
        if required_libraries is None:
            required_libraries = self.find_required_libraries(ignore_package)
        unmanaged_libraries = library.list_unmanaged()
        return installed_libraries - required_libraries - unmanaged_libraries

    def _download_zip_file(self, name, url):
        try:
            return zipfile.ZipFile(BytesIO(http_get(url, self.settings, '')))

        except DownloaderException as e:
            console_write(
                '''
                Unable to download "%s": %s
                ''',
                (name, e)
            )
            return False

        except zipfile.BadZipfile:
            console_write(
                '''
                Failed to unzip the file for "%s"
                ''',
                name
            )
            return False

    def _common_folder(self, name, zf):
        """
        If all files in a zip file are contained in a single folder

        :param name:
            The name of the package or library

        :param zf:
            The zipfile instance

        :return:
            False if an error occurred, or a unicode string of the common
            folder name. If no common folder, a blank string is returned. If
            a folder name is returned, it will end in "/".
        """

        root_level_paths = []
        last_path = None
        for path in zf.namelist():
            try:
                if not isinstance(path, str):
                    path = path.decode('utf-8', 'strict')
            except (UnicodeDecodeError):
                console_write(
                    '''
                    One or more of the zip file entries in "%s" is not
                    encoded using UTF-8, aborting
                    ''',
                    name
                )
                return False

            last_path = path

            if path.find('/') in [len(path) - 1, -1]:
                root_level_paths.append(path)
            # Make sure there are no paths that look like security vulnerabilities
            if path[0] == '/' or path.find('../') != -1 or path.find('..\\') != -1:
                console_write(
                    '''
                    The zip file for "%s" contains files that traverse outside
                    of the root and cannot be safely installed
                    ''',
                    name
                )
                return False

        if last_path and len(root_level_paths) == 0:
            root_level_paths.append(last_path[0:last_path.find('/') + 1])

        # If there is only a single directory at the top leve, the file
        # is most likely a zip from BitBucket or GitHub and we need
        # to skip the top-level dir when extracting
        if len(root_level_paths) == 1 and root_level_paths[0].endswith('/'):
            return root_level_paths[0]

        return ""

    def _extract_zip(self, name, zf, dest_dir, extracted_dirs, extracted_files, common_folder):
        """
        Extracts a zip to a folder

        :param name:
            A unicode string of the package or library name

        :param zf:
            A zipfile instance to extract

        :param dest_dir:
            A unicode string of the destination directory

        :param extracted_dirs:
            A set of all of the dir paths extracted from the zip

        :param extracted_files:
            A set of all of the files paths extracted from the zip

        :param common_folder:
            A unicode string of a common folder name

        :return:
            A bool indication if the install should be retried
        """

        # Here we don't use .extractall() since it was having issues on OS X
        should_retry = False
        for info in zf.infolist():
            path = info.filename
            dest = path

            try:
                if not isinstance(dest, str):
                    dest = dest.decode('utf-8', 'strict')
            except (UnicodeDecodeError):
                console_write(
                    '''
                    One or more of the zip file entries in "%s" is not
                    encoded using UTF-8, aborting
                    ''',
                    name
                )
                return False

            if os.name == 'nt':
                regex = r':|\*|\?|"|<|>|\|'
                if re.search(regex, dest) is not None:
                    console_write(
                        '''
                        Skipping file from package named "%s" due to an
                        invalid filename
                        ''',
                        name
                    )
                    continue

            # If there was only a single directory in the package, we remove
            # that folder name from the paths as we extract entries
            dest = sys_path.longpath(os.path.join(dest_dir, dest[len(common_folder):]))

            def add_extracted_dirs(dir_):
                while dir_ not in extracted_dirs:
                    extracted_dirs.add(dir_)
                    dir_ = os.path.dirname(dir_)
                    if dir_ == dest_dir:
                        break

            if path.endswith('/'):
                os.makedirs(dest, exist_ok=True)
                add_extracted_dirs(dest)
            else:
                parent_dir = os.path.dirname(dest)
                os.makedirs(parent_dir, exist_ok=True)
                add_extracted_dirs(parent_dir)
                extracted_files.add(dest)
                try:
                    with open(dest, 'wb') as fobj:
                        fobj.write(zf.read(path))
                except (IOError) as e:
                    if e.errno == 13:  # permission denied
                        should_retry = True
                        break
                    console_write(
                        '''
                        Skipping file from package named "%s" due to an
                        invalid filename
                        ''',
                        name
                    )

                except (UnicodeDecodeError):
                    console_write(
                        '''
                        Skipping file from package named "%s" due to an
                        invalid filename
                        ''',
                        name
                    )
        return should_retry

    def install_libraries(self, libraries, fail_early=True):
        """
        Ensures a list of libraries are installed and up-to-date

        :param libraries:
            A list of library.Library() objects

        :param fail_early:
            Whether to abort installation if a library installation fails.

        :return:
            A boolean indicating if the libraries are properly installed
        """

        error = False
        for lib in libraries:
            if not self.install_library(lib):
                if fail_early:
                    return False
                error = True

        return not error

    def install_library(self, lib):
        """
        Install a library

        :param lib:
            The library.Library object to install

        :returns:
            True, if the library is successfully installed or upgraded
            False, if library could not be installed
        """

        debug = self.settings.get('debug')

        installed_version = None
        installed_library = library.find_installed(lib)
        if installed_library:
            installed_version = installed_library.dist_info.read_metadata().get('version')
            if installed_version:
                installed_version = pep440.PEP440Version(installed_version)

        is_upgrade = installed_library is not None

        available_libraries = self.list_available_libraries(lib.python_version)
        try:
            available_library = available_libraries[lib.name]
        except (IndexError, KeyError):
            if lib.name in self.settings.get('unavailable_libraries', []):
                console_write(
                    '''
                    The library "%s" is either not available on this platform,
                    for Python %s, or for this version of Sublime Text
                    ''',
                    (lib.name, lib.python_version)
                )
            else:
                console_write(
                    'The library "%s" is not available for Python %s',
                    (lib.name, lib.python_version)
                )
            return False

        try:
            release = available_library['releases'][0]
            available_version = pep440.PEP440Version(release['version'])
        except (IndexError, KeyError):
            if is_upgrade:
                console_write(
                    '''
                    The library "%s" for Python %s is installed,
                    but the latest available release
                    could not be determined; leaving alone
                    ''',
                    (lib.name, lib.python_version)
                )
                return True

            console_write(
                '''
                The latest available release of library "%s" for Python %s
                could not be determined
                ''',
                (lib.name, lib.python_version)
            )
            return False

        if is_upgrade:
            if installed_version >= available_version:
                if debug:
                    console_write(
                        'The library "%s" for Python %s is installed and up to date',
                        (lib.name, lib.python_version)
                    )
                return True

            _, modified_ris = installed_library.dist_info.verify_files()
            modified_paths = {mri.absolute_path for mri in modified_ris}
            if modified_paths:
                console_write(
                    'Unable to upgrade library "%s" because files on disk have been modified: "%s"',
                    (lib.name, '", "'.join(sorted(modified_paths, key=lambda s: s.lower())))
                )
                return False

        lib_path = sys_path.lib_paths()[lib.python_version]
        tmp_dir = sys_path.longpath(tempfile.mkdtemp(''))
        tmp_library_dir = os.path.join(tmp_dir, lib.name)

        # This is refers to the zipfile later on, so we define it here so we can
        # close the zip file if set during the finally clause
        library_zip = None

        try:
            library_zip = self._download_zip_file(lib.name, release['url'])
            if library_zip is False:
                return False

            common_folder = self._common_folder(lib.name, library_zip)
            if common_folder is False:
                return False

            library_names = release.get('libraries')
            if not library_names:
                # If libraries were not in the channel, try the package
                try:
                    lib_info_json = library_zip.read(common_folder + 'dependencies.json')
                    lib_info = json.loads(lib_info_json.decode('utf-8'))
                except (KeyError):
                    lib_info = {}
                except (ValueError):
                    console_write(
                        '''
                        Failed to parse the dependencies.json of
                        library "%s" for Python %s
                        ''',
                        (lib.name, lib.python_version)
                    )
                    return False

                library_names = self.select_libraries(lib_info)

            if library_names:
                self.install_libraries(
                    (library.Library(name, lib.python_version) for name in library_names),
                    fail_early=False
                )

            os.mkdir(tmp_library_dir)

            extracted_files = set()
            extracted_dirs = set()
            should_retry = self._extract_zip(
                lib.name,
                library_zip,
                tmp_library_dir,
                extracted_dirs,
                extracted_files,
                common_folder
            )

            if should_retry:
                return False

            extracted_paths = extracted_files | extracted_dirs

            library_zip.close()
            library_zip = None

            new_did_name = '%s-%s.dist-info' % (lib.name, release['version'])
            wheel_filename = new_did_name + '/WHEEL'
            is_whl = wheel_filename in extracted_paths

            if is_whl:
                temp_did = library.distinfo.DistInfoDir(tmp_library_dir, new_did_name)

                _, modified_ris = temp_did.verify_files()
                modified_paths = {mri.absolute_path for mri in modified_ris}
                if modified_paths:
                    console_write(
                        'Unable to %s library "%s" because files in the archive have been modified: "%s"',
                        (
                            'upgrade' if is_upgrade else 'install',
                            lib.name,
                            '", "'.join(sorted(modified_paths, key=lambda s: s.lower()))
                        )
                    )
                    return False

                try:
                    temp_did.verify_python_version(lib.python_version)
                except EnvironmentError as e:
                    console_write(e)
                    return False

            else:
                try:
                    temp_did = library.convert_dependency(
                        tmp_library_dir,
                        lib.python_version,
                        lib.name,
                        release['version'],
                        available_library.get('description'),
                        available_library.get('homepage')
                    )
                except ValueError as e:
                    console_write(
                        '''
                        Failed to install the library "%s": %s
                        ''',
                        (lib.name, e)
                    )
                    return False

            if is_upgrade:
                try:
                    library.remove(installed_library)
                except OSError as e:
                    console_write(
                        '''
                        Failed to upgrade the library "%s": %s
                        ''',
                        (lib.name, e)
                    )
                    return False

            library.install(temp_did, lib_path)

            if is_upgrade:
                console_write(
                    'Upgraded library "%s" to %s for Python %s',
                    (lib.name, release['version'], lib.python_version))
            else:
                console_write(
                    'Installed library "%s" %s for Python %s',
                    (lib.name, release['version'], lib.python_version))

            return True

        finally:
            # We need to make sure the zipfile is closed to
            # help prevent permissions errors on Windows
            if library_zip:
                library_zip.close()

            # Try to remove the tmp dir after a second to make sure
            # a virus scanner is holding a reference to the zipfile
            # after we close it.
            sublime.set_timeout(lambda: delete_directory(tmp_dir), 1000)

    def cleanup_libraries(self, ignore_package=None, required_libraries=None):
        """
        Remove all not needed libraries by the installed packages,
        ignoring the specified package.

        :param ignore_package:
            The package to ignore when enumerating libraries.
            Not used when required_libraries is provided.

        :param required_libraries:
            All required libraries, for speedup purposes.

        :return:
            Boolean indicating the success of the removals.
        """

        orphaned_libraries = self.find_orphaned_libraries(ignore_package, required_libraries)

        error = False
        for lib in orphaned_libraries:
            if not self.remove_library(lib):
                error = True

        return not error

    def remove_library(self, lib):
        """
        Deletes a library

        :param lib:
            The library.InstalledLibrary() to delete

        :return:
            bool if the library was successfully deleted
        """

        try:
            library.remove(lib)

        except library.distinfo.DistInfoNotFoundError:
            console_write(
                '''
                The library specified, "%s" for Python %s, is not installed
                ''',
                (lib.name, lib.python_version)
            )
            return False

        except OSError:
            # THe way library.remove() works is that the .dist-info dir is
            # removed last. This means that any permissions errors will happen
            # before we remove the metadata, and thus we'll still think the
            # library is installed when ST restarts, and we can try removing
            # it again in the future.
            console_write(
                '''
                Failed to remove the library %s for Python %s -
                deferring until next start
                ''',
                (lib.name, lib.python_version)
            )
            return False

        else:
            console_write(
                '''
                The orphaned library %s for Python %s has been removed
                ''',
                (lib.name, lib.python_version)
            )
            return True

    def install_package(self, package_name):
        """
        Downloads and installs (or upgrades) a package

        Uses the self.list_available_packages() method to determine where to
        retrieve the package file from.

        The install process consists of:

        1. Finding the package
        2. Downloading the .sublime-package/.zip file
        3. Extracting the package file
        4. Showing install/upgrade messaging
        5. Submitting usage info
        6. Recording that the package is installed

        :param package_name:
            The package to download and install

        :return: bool if the package was successfully installed or None
                 if the package needs to be cleaned up on the next restart
                 and should not be reenabled
        """

        # Handle VCS packages first as those might not be registered
        # in one of the repositories or channels.
        upgrader = self.instantiate_upgrader(package_name)
        if upgrader:
            # We explicitly don't support the "libraries" key when dealing
            # with packages installed via VCS
            to_ignore = self.settings.get('ignore_vcs_packages')
            if to_ignore is True:
                console_write(
                    '''
                    Skipping %s package "%s" since the setting
                    "ignore_vcs_packages" is set to true
                    ''',
                    (upgrader.cli_name, package_name)
                )
                return False

            if isinstance(to_ignore, list) and package_name in to_ignore:
                console_write(
                    '''
                    Skipping %s package "%s" since it is listed in the
                    "ignore_vcs_packages" setting
                    ''',
                    (upgrader.cli_name, package_name)
                )
                return False

            result = upgrader.run()

            # We are done here, if the package is an unmanaged VCS package.
            # Otherwise the package might just be an override.
            if not zip_file_exists(package_name, 'package-metadata.json'):
                console_write('Upgraded %s', package_name)
                return result

        # package is to be renamed during upgrade
        old_package_name = package_name
        package_name = self.settings.get('renamed_packages', {}).get(package_name) or package_name

        packages = self.list_available_packages()
        if package_name not in packages:
            if package_name in self.settings.get('unavailable_packages', []):
                console_write(
                    '''
                    The package "%s" is either not available on this platform or for
                    this version of Sublime Text
                    ''',
                    package_name
                )
            else:
                console_write('The package "%s" is not available', package_name)

            return False

        package = packages[package_name]
        release = package['releases'][0]

        unpacked_package_dir = get_package_dir(package_name)
        package_path = get_installed_package_path(package_name)
        package_filename = os.path.basename(package_path)

        tmp_dir = sys_path.longpath(tempfile.mkdtemp(''))
        tmp_package_path = os.path.join(tmp_dir, package_filename)

        # This is refers to the zipfile later on, so we define it here so we can
        # close the zip file if set during the finally clause
        package_zip = None

        try:
            old_metadata = self.get_metadata(old_package_name)
            old_version = old_metadata.get('version')
            is_upgrade = old_version is not None

            package_zip = self._download_zip_file(package_name, release['url'])
            if package_zip is False:
                return False

            common_folder = self._common_folder(package_name, package_zip)
            if common_folder is False:
                return False

            # By default, ST prefers .sublime-package files since this allows
            # overriding files in the Packages/{package_name}/ folder.
            # If the package maintainer doesn't want a .sublime-package
            try:
                package_zip.getinfo(common_folder + '.no-sublime-package')
                unpack = True
            except (KeyError):
                unpack = False

            python_version = "3.3"
            supported_python_versions = set(sys_path.lib_paths().keys())

            try:
                python_version_file = common_folder + '.python-version'
                python_version_raw = package_zip.read(python_version_file).decode('utf-8').strip()
                if python_version_raw in supported_python_versions:
                    python_version = python_version_raw
            except (KeyError):
                # no .python-version found in archive,
                # get best matching python version from upstream release data
                python_versions = release.get("python_versions")
                if python_versions:
                    python_version_raw = str(max(map(pep440.PEP440Version, set(python_versions) & supported_python_versions)))
                    if python_version_raw:
                        python_version = python_version_raw

            # Try to read .python-version from existing unpacked package directory to respect local
            # opt-in to certain plugin_host and to install correct libraries.
            try:
                python_version_file = os.path.join(get_package_dir(old_package_name), '.python-version')
                with open(python_version_file, 'r', encoding='utf-8') as fobj:
                    python_version_raw = fobj.read().strip()
                    if python_version_raw in supported_python_versions and (
                        unpack or pep440.PEP440Version(python_version_raw) > pep440.PEP440Version(python_version)
                    ):
                        python_version = python_version_raw
            except (FileNotFoundError):
                pass

            library_names = release.get('libraries')
            if not library_names:
                # If libraries were not in the channel, try the package
                try:
                    lib_info_json = package_zip.read(common_folder + 'dependencies.json')
                    lib_info = json.loads(lib_info_json.decode('utf-8'))
                except (KeyError):
                    lib_info = {}
                except (ValueError):
                    console_write(
                        '''
                        Failed to parse the dependencies.json for "%s"
                        ''',
                        package_name
                    )
                    return False

                library_names = self.select_libraries(lib_info)

            if library_names:
                self.install_libraries(
                    (library.Library(name, python_version) for name in library_names),
                    fail_early=False
                )

            if package_name != old_package_name:
                self.rename_package(old_package_name, package_name)

            # If we determined it should be unpacked, we extract directly
            # into the Packages/{package_name}/ folder
            if unpack:
                # Make sure not to overwrite existing hidden packages or package overrides
                #
                # A hidden unpacked package is expected to have been created locally,
                # either manually by user or dynamically by a plugin.
                #
                # It may serve as:
                # a) override for a *.sublime-package file.
                # b) invisible helper package, which can't be enabled/disabled/removed
                #    by user via API/GUI (if no corresponding *.sublime-package file exists)
                if regular_file_exists(package_name, '.hidden-sublime-package'):
                    console_write(
                        '''
                        Failed to %s %s -
                        Overwriting existing hidden package not allowed.
                        ''',
                        ('upgrade' if is_upgrade else 'install', package_name)
                    )
                    return False

                if not self.backup_package_dir(package_name):
                    return False

                package_dir = unpacked_package_dir

            # Otherwise we go into a temp dir since we will be creating a
            # new .sublime-package file later
            else:
                # If we already have a package-metadata.json file in
                # Packages/{package_name}/, but the package no longer contains
                # a .no-sublime-package file, then we want to clear the unpacked
                # dir and install as a .sublime-package file. Since we are only
                # clearing if a package-metadata.json file exists, we should never
                # accidentally delete user's customizations. However, we still
                # create a backup just in case.
                if regular_file_exists(package_name, 'package-metadata.json'):
                    if not self.backup_package_dir(package_name):
                        return False

                    if not delete_directory(unpacked_package_dir):
                        # If deleting failed, queue the package to upgrade upon next start
                        # when it will be disabled
                        reinstall_file = os.path.join(unpacked_package_dir, 'package-control.reinstall')
                        create_empty_file(reinstall_file)
                        console_write(
                            '''
                            Failed to upgrade %s -
                            deferring until next start
                            ''',
                            package_name
                        )
                        return None

                tmp_working_dir = os.path.join(tmp_dir, 'working')
                os.mkdir(tmp_working_dir)
                package_dir = tmp_working_dir

            package_metadata_file = os.path.join(package_dir, 'package-metadata.json')

            if not os.path.exists(package_dir):
                os.mkdir(package_dir)

            extracted_files = set()
            extracted_dirs = set()
            should_retry = self._extract_zip(
                package_name,
                package_zip,
                package_dir,
                extracted_dirs,
                extracted_files,
                common_folder
            )
            extracted_paths = extracted_dirs | extracted_files

            package_zip.close()
            package_zip = None

            # If upgrading failed, queue the package to upgrade upon next start
            if should_retry:
                reinstall_file = os.path.join(package_dir, 'package-control.reinstall')
                create_empty_file(reinstall_file)

                # Don't delete the metadata file, that way we have it
                # when the reinstall happens, and the appropriate
                # usage info can be sent back to the server.
                # No need to handle symlink at this stage it was already removed
                # and we are not working with symlink here anymore.
                clear_directory(package_dir, {reinstall_file, package_metadata_file})

                console_write(
                    '''
                    Failed to upgrade %s -
                    deferring until next start
                    ''',
                    package_name
                )
                return None

            # Here we clean out any files that were not just overwritten. It is ok
            # if there is an error removing a file. The next time there is an
            # upgrade, it should be cleaned out successfully then.
            # No need to handle symlink at this stage it was already removed
            # and we are not working with symlink here anymore.
            clear_directory(package_dir, extracted_paths)

            # Create .python-version file to opt-in to certain plugin_host.
            # It enables unmaintained packages/plugins to be opted-in to newer python version
            # via upstream release information or via local settings.
            if python_version != '3.3':
                try:
                    python_version_file = os.path.join(package_dir, '.python-version')
                    with open(python_version_file, 'x') as fobj:
                        fobj.write(python_version)
                except FileExistsError:
                    pass

            new_version = release['version']

            self.print_messages(package_name, package_dir, is_upgrade, old_version, new_version)

            with open(package_metadata_file, 'w', encoding='utf-8') as fobj:
                now = time.time()
                install_time = old_metadata.get("install_time", now)
                metadata = {
                    "version": new_version,
                    "install_time": install_time,
                    "sublime_text": release['sublime_text'],
                    "platforms": release['platforms'],
                    "url": package['homepage'],
                    "description": packages[package_name]['description'],
                    'libraries': release.get('libraries', [])
                }
                if is_upgrade:
                    metadata['upgrade_time'] = now
                json.dump(metadata, fobj)

            # Submit install and upgrade info
            if is_upgrade:
                params = {
                    'package': package_name,
                    'operation': 'upgrade',
                    'version': new_version,
                    'old_version': old_version
                }
            else:
                params = {
                    'package': package_name,
                    'operation': 'install',
                    'version': new_version
                }
            self.record_usage(params)

            # Record the install in the settings file so that you can move
            # settings across computers and have the same packages installed
            self.update_installed_packages(
                add=package_name,
                remove=old_package_name if package_name != old_package_name else None,
                persist=False
            )

            # If we extracted directly into the Packages/{package_name}/
            # we probably need to remove an old Installed Packages/{package_name].sublime-package
            if unpack:
                try:
                    os.remove(package_path)
                except (FileNotFoundError):
                    pass
                except (OSError) as e:
                    console_write(
                        '''
                        Unable to remove "%s" after upgrade to unpacked package: %s
                        ''',
                        (package_filename, e)
                    )

            # If we didn't extract directly into the Packages/{package_name}/
            # folder, we need to create a .sublime-package file and install it
            else:
                try:
                    with zipfile.ZipFile(tmp_package_path, "w", compression=zipfile.ZIP_DEFLATED) as fobj:
                        for root, _, files in os.walk(package_dir):
                            for file in files:
                                full_path = os.path.join(root, file)
                                relative_path = os.path.relpath(full_path, package_dir)
                                fobj.write(full_path, relative_path)

                except (OSError, IOError) as e:
                    console_write(
                        '''
                        Failed to create the package file "%s" in %s: %s
                        ''',
                        (package_filename, tmp_dir, e)
                    )
                    return False

                try:
                    try:
                        os.remove(package_path)
                    except (FileNotFoundError):
                        pass
                    shutil.move(tmp_package_path, package_path)

                except (OSError):
                    try:
                        shutil.move(tmp_package_path, package_path + '-new')
                    except (OSError):
                        pass

                    console_write(
                        '''
                        Failed to upgrade %s -
                        deferring until next start
                        ''',
                        package_name
                    )
                    return None

            if is_upgrade:
                console_write('Upgraded %s to %s', (package_name, new_version))
            else:
                console_write('Installed %s %s', (package_name, new_version))

            return True

        finally:
            # We need to make sure the zipfile is closed to
            # help prevent permissions errors on Windows
            if package_zip:
                package_zip.close()

            # Try to remove the tmp dir after a second to make sure
            # a virus scanner is holding a reference to the zipfile
            # after we close it.
            sublime.set_timeout_async(lambda: delete_directory(tmp_dir), 1000)

    def rename_package(self, package_name, new_package_name):
        """
        Rename a package

        :param package_name:
            The package name
        :param new_package_name:
            The new package name

        :returns:
            ``True`` on success
            ``False`` if package can not be renamed
        """

        # User package needs to be checked as it exists in Data/Packages/
        if package_name.lower() == 'user' or new_package_name.lower() == 'user':
            console_write(
                '''
                The package "%s" can not be renamed
                ''',
                package_name
            )
            return False

        case_insensitive_fs = self.settings['platform'] in ['windows', 'osx']
        changing_case = case_insensitive_fs and package_name.lower() == new_package_name.lower()

        def do_rename(old, new):
            # Windows will not allow you to rename to the same name with
            # a different case, so we work around that with a temporary name
            if changing_case:
                temp_path = os.path.join(os.path.dirname(sublime.packages_path()), '__' + new)
                os.rename(old, temp_path)
                old = temp_path

            os.rename(old, new)

        package_file = get_installed_package_path(package_name)

        try:
            do_rename(package_file, get_installed_package_path(new_package_name))
        except FileNotFoundError:
            # package file does not exist, nothing to do
            pass
        except FileExistsError:
            # delete source file if destination already exists
            try:
                os.remove(package_file)
            except (OSError, IOError) as e:
                if self.settings.get('debug'):
                    console_write(
                        '''
                        Unable to remove package "%s" -
                        deferring until next start: %s
                        ''',
                        (package_name, e)
                    )

        package_dir = get_package_dir(package_name)

        try:
            do_rename(package_dir, get_package_dir(new_package_name))
        except FileNotFoundError:
            # package dir does not exist, nothing to do
            pass
        except FileExistsError:
            # delete source dir if destination already exists
            if not self.backup_package_dir(package_name):
                console_write('It is therefore not removed automatically.')

            elif not delete_directory(package_dir):
                if self.settings.get('debug'):
                    console_write(
                        '''
                        Unable to remove directory for package "%s" -
                        deferring until next start
                        ''',
                        package_name
                    )
                create_empty_file(os.path.join(package_dir, 'package-control.cleanup'))

        # Remove optionally present cache if exists
        # ST will recreate cache for renamed packages, automatically
        delete_directory(get_package_cache_dir(package_name))
        delete_directory(get_package_module_cache_dir(package_name))

        return True

    def remove_package(self, package_name):
        """
        Deletes a package

        The deletion process consists of:

        1. Removing the package from the list of installed packages
        2. Deleting the directory (or marking it for deletion if deletion fails)
        3. Submitting usage info

        :param package_name:
            The package to delete

        :return:
            ``True`` if the package was successfully deleted
            ``False`` if the package doesn't exist or can not be deleted
            ``None`` if the package needs to be cleaned up on the next restart
            and should not be reenabled
        """

        self.update_installed_packages(remove=package_name, persist=False)

        version = self.get_metadata(package_name).get('version')

        result = self.delete_package(package_name)
        if result is not False:
            self.record_usage({
                'package': package_name,
                'operation': 'remove',
                'version': version
            })

            # Remove libraries that are no longer needed
            self.cleanup_libraries(package_name)

        return result

    def delete_package(self, package_name):
        """
        Delete package resources from filesystem.

        The method removes all package related files and directories without
        manipulating any metadata such as installed_packages or uploading usage data.

        :param package_name:
            The package to delete

        :return:
            ``True`` if the package was successfully deleted
            ``False`` if the package doesn't exist or can not be deleted
            ``None`` if the package needs to be cleaned up on the next restart
            and should not be reenabled
        """

        # User package needs to be checked as it exists in Data/Packages/
        if package_name.lower() == 'user':
            console_write(
                '''
                The package "%s" can not be removed
                ''',
                package_name
            )
            return False

        package_file = get_installed_package_path(package_name)
        package_dir = get_package_dir(package_name)

        can_delete_file = os.path.exists(package_file)
        can_delete_dir = os.path.exists(package_dir)

        if not can_delete_file and not can_delete_dir:
            console_write(
                '''
                The package "%s" is not installed
                ''',
                package_name
            )
            return False

        result = True

        if can_delete_file:
            try:
                os.remove(package_file)
            except (OSError, IOError) as e:
                if self.settings.get('debug'):
                    console_write(
                        '''
                        Unable to remove package "%s" -
                        deferring until next start: %s
                        ''',
                        (package_name, e)
                    )
                result = None

        if can_delete_dir:
            if not self.backup_package_dir(package_name):
                console_write('It is therefore not removed automatically.')

            elif not delete_directory(package_dir):
                if self.settings.get('debug'):
                    console_write(
                        '''
                        Unable to remove directory for package "%s" -
                        deferring until next start
                        ''',
                        package_name
                    )
                create_empty_file(os.path.join(package_dir, 'package-control.cleanup'))
                result = None

        # remove optionally present cache if exists
        delete_directory(get_package_cache_dir(package_name))
        delete_directory(get_package_module_cache_dir(package_name))

        message = 'The package "%s" has been removed' % package_name
        if result is None:
            message += ' and will be cleaned up on the next restart'
        console_write(message)

        return result

    def backup_package_dir(self, package_name):
        """
        Does a full backup of the Packages/{package}/ dir to Backup/

        :param package_name:
            The name of the package to back up

        :return:
            If the backup succeeded
        """

        package_dir = get_package_dir(package_name)
        if not os.path.exists(package_dir):
            return True

        backup_dir = os.path.join(
            sys_path.data_path(), 'Backup', datetime.datetime.now().strftime('%Y%m%d%H%M%S')
        )
        package_backup_dir = os.path.join(backup_dir, package_name)

        try:
            if os.path.exists(package_backup_dir):
                console_write(
                    '''
                    Backup folder "%s" already exists!
                    ''',
                    package_backup_dir
                )
            else:
                os.makedirs(backup_dir, exist_ok=True)
            shutil.copytree(package_dir, package_backup_dir)
            return True

        except (OSError, IOError) as e:
            delete_directory(package_backup_dir)
            console_write(
                '''
                Failed to backup the package directory for "%s": %s
                ''',
                (package_name, e)
            )
            return False

    def prune_backup_dir(self):
        """
        Remove all backups older than ``max_backup_age`` days.
        """

        age = max(0, self.settings.get('max_backup_age', 14))
        today = datetime.date.today()
        backup_dir = os.path.join(sys_path.data_path(), 'Backup')

        if not os.path.isdir(backup_dir):
            return

        for fname in os.listdir(backup_dir):
            package_backup_dir = os.path.join(backup_dir, fname)
            if not os.path.isdir(package_backup_dir):
                continue

            try:
                date = datetime.date(int(fname[:4]), int(fname[4:6]), int(fname[6:8]))
                if (today - date).days > age:
                    delete_directory(package_backup_dir)
            except ValueError:
                continue

    def print_messages(self, package_name, package_dir, is_upgrade, old_version, new_version):
        """
        Prints out package install and upgrade messages

        The functionality provided by this allows package maintainers to
        show messages to the user when a package is installed, or when
        certain version upgrade occur.

        :param package_name:
            The name of the package the message is for

        :param package_dir:
            The full filesystem path to the package directory

        :param is_upgrade:
            If the install was actually an upgrade

        :param old_version:
            The string version of the package before the upgrade occurred

        :param new_version:
            The new (string) version of the package
        """

        try:
            messages_file = os.path.join(package_dir, 'messages.json')
            with open(messages_file, 'r', encoding='utf-8') as fobj:
                message_info = json.load(fobj)
        except (FileNotFoundError):
            return
        except (ValueError):
            console_write(
                '''
                Error parsing messages.json for %s
                ''',
                package_name
            )
            return

        def read_message(message_path):
            with open(sys_path.longpath(message_path), 'r', encoding='utf-8', errors='replace') as fobj:
                return '\n  %s\n' % fobj.read().rstrip().replace('\n', '\n  ')

        output = ''
        if not is_upgrade:
            install_file = message_info.get('install')
            if install_file:
                try:
                    install_path = os.path.join(package_dir, install_file)
                    output += read_message(install_path)
                except (FileNotFoundError):
                    console_write(
                        '''
                        Error opening install message for %s from %s
                        ''',
                        (package_name, install_file)
                    )

        elif is_upgrade and old_version:
            upgrade_messages = list(set(message_info.keys()) - set(['install']))
            upgrade_messages = version_sort(upgrade_messages, reverse=True)
            old_version_cmp = PackageVersion(old_version)
            new_version_cmp = PackageVersion(new_version)

            for version in upgrade_messages:
                version_cmp = PackageVersion(version)
                if version_cmp <= old_version_cmp:
                    break
                # If the package developer sets up release notes for future
                # versions, we don't want to show them for every release
                if version_cmp > new_version_cmp:
                    continue

                upgrade_file = message_info.get(version)
                upgrade_path = os.path.join(package_dir, upgrade_file)

                try:
                    output += read_message(upgrade_path)
                except (FileNotFoundError):
                    console_write(
                        '''
                        Error opening %s message for %s from %s
                        ''',
                        (version, package_name, upgrade_file)
                    )

        if not output:
            return
        else:
            output = '\n\n%s\n%s\n' % (package_name, '-' * len(package_name)) + output

        def print_to_panel():
            window = sublime.active_window()

            views = window.views()
            view = None
            for _view in views:
                if _view.name() == 'Package Control Messages':
                    view = _view
                    break

            if not view:
                view = window.new_file()
                view.set_name('Package Control Messages')
                view.set_scratch(True)
                view.settings().set("word_wrap", True)
                view.settings().set("auto_indent", False)
                view.settings().set("tab_width", 2)
            else:
                view.set_read_only(False)
                if window.active_view() != view:
                    window.focus_view(view)

            def write(string):
                view.run_command('insert', {'characters': string})

            old_sel = list(view.sel())
            old_vpos = view.viewport_position()

            size = view.size()
            view.sel().clear()
            view.sel().add(sublime.Region(size, size))

            if not view.size():
                write(text.format(
                    '''
                    Package Control Messages
                    ========================
                    '''
                ))
            write(output)

            # Move caret to the new end of the file if it was previously
            if sublime.Region(size, size) == old_sel[-1]:
                old_sel[-1] = sublime.Region(view.size(), view.size())

            view.sel().clear()
            for reg in old_sel:
                view.sel().add(reg)

            view.set_viewport_position(old_vpos, False)
            view.set_read_only(True)

        sublime.set_timeout(print_to_panel, 1)

    def record_usage(self, params):
        """
        Submits install, upgrade and delete actions to a usage server

        The usage information is currently displayed on the Package Control
        website at https://packagecontrol.io

        :param params:
            A dict of the information to submit
        """

        if not self.settings.get('submit_usage'):
            return

        params['package_control_version'] = self.get_metadata('Package Control').get('version')
        params['sublime_platform'] = self.settings.get('platform')
        params['sublime_version'] = self.settings.get('version')

        # For Python 2, we need to explicitly encoding the params
        for param in params:
            if isinstance(params[param], str):
                params[param] = params[param].encode('utf-8')

        url = self.settings.get('submit_url', '') + '?' + urlencode(params)

        try:
            result = http_get(url, self.settings, 'Error submitting usage information.')
        except (DownloaderException) as e:
            console_write(e)
            return

        try:
            result = json.loads(result.decode('utf-8'))
            if result['result'] != 'success':
                raise ValueError()
        except (ValueError):
            console_write(
                '''
                Error submitting usage information for %s
                ''',
                params['package']
            )
