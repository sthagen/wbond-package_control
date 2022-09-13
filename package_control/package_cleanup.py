import functools
import os
import threading
import time

import sublime

from . import library, sys_path, text, __version__
from .automatic_upgrader import AutomaticUpgrader
from .clear_directory import clear_directory, delete_directory
from .console_write import console_write
from .package_disabler import PackageDisabler
from .package_io import create_empty_file, get_installed_package_path, get_package_dir, package_file_exists
from .package_manager import PackageManager
from .selectors import is_compatible_platform, is_compatible_version
from .settings import preferences_filename, pc_settings_filename, load_list_setting, save_list_setting
from .show_error import show_error


class PackageCleanup(threading.Thread):

    """
    Cleans up folders for packages that were removed, but that still have files
    in use.
    """

    def __init__(self):
        self.manager = PackageManager()

        settings = sublime.load_settings(pc_settings_filename())

        # We no longer use the installed_dependencies setting because it is not
        # necessary and created issues with settings shared across operating systems
        if settings.get('installed_dependencies'):
            settings.erase('installed_dependencies')
            sublime.save_settings(pc_settings_filename())

        self.original_installed_packages = load_list_setting(settings, 'installed_packages')
        self.remove_orphaned = settings.get('remove_orphaned', True)

        threading.Thread.__init__(self)

    def run(self):
        # This song and dance is necessary so Package Control doesn't try to clean
        # itself up, but also get properly marked as installed in the settings
        installed_packages_at_start = set(self.original_installed_packages)

        # Ensure we record the installation of Package Control itself
        if 'Package Control' not in installed_packages_at_start:
            params = {
                'package': 'Package Control',
                'operation': 'install',
                'version': __version__
            }
            self.manager.record_usage(params)
            installed_packages_at_start.add('Package Control')

        found_packages = set()
        installed_packages = set(installed_packages_at_start)

        for file in os.listdir(sys_path.installed_packages_path):
            package_name, file_extension = os.path.splitext(file)
            file_extension = file_extension.lower()

            # If there is a package file ending in .sublime-package-new, it
            # means that the .sublime-package file was locked when we tried
            # to upgrade, so the package was left in ignored_packages and
            # the user was prompted to restart Sublime Text. Now that the
            # package is not loaded, we can replace the old version with the
            # new one.
            if file_extension == '.sublime-package-new':
                new_file = os.path.join(sys_path.installed_packages_path, file)
                package_file = get_installed_package_path(package_name)
                try:
                    try:
                        os.remove(package_file)
                    except FileNotFoundError:
                        pass

                    os.rename(new_file, package_file)
                    console_write(
                        '''
                        Finished replacing %s.sublime-package
                        ''',
                        package_name
                    )

                except OSError as e:
                    console_write(
                        '''
                        Failed to replace %s.sublime-package with new package. %s
                        ''',
                        (package_name, e)
                    )

                found_packages.add(package_name)

            elif file_extension == '.sublime-package':
                found_packages.add(package_name)

        installed_libraries = self.manager.list_libraries()
        required_libraries = self.manager.find_required_libraries()
        unmanaged_libraries = library.list_unmanaged()
        extra_libraries = sorted(set(installed_libraries) - set(required_libraries) - set(unmanaged_libraries))

        found_libraries = set(installed_libraries)

        # Clean up unneeded libraries so that found_libraries will only
        # end up having required libraries added to it
        for lib in extra_libraries:
            try:
                library.remove(sys_path.lib_paths()[lib.python_version], lib.name)
                console_write(
                    '''
                    Removed unneeded library %s for Python %s
                    ''',
                    (lib.name, lib.python_version)
                )
                found_libraries.remove(lib)

            except OSError:
                console_write(
                    '''
                    Unable to remove unneeded library %s for Python %s -
                    deferring until next start
                    ''',
                    (lib.name, lib.python_version)
                )

        found_libraries = sorted(found_libraries)

        for package_name in os.listdir(sys_path.packages_path):

            # Ignore `.`, `..` or hidden dot-directories
            if package_name[0] == '.':
                continue

            # Make sure not to clear user settings under all circumstances
            if package_name.lower() == 'user':
                continue

            # Ignore files
            package_dir = os.path.join(sys_path.packages_path, package_name)
            if not os.path.isdir(package_dir):
                continue

            # Cleanup packages that could not be removed due to in-use files
            cleanup_file = os.path.join(package_dir, 'package-control.cleanup')
            if os.path.exists(cleanup_file):
                if delete_directory(package_dir):
                    console_write(
                        '''
                        Removed old package directory %s
                        ''',
                        package_name
                    )

                else:
                    create_empty_file(cleanup_file)
                    console_write(
                        '''
                        Unable to remove old package directory %s -
                        deferring until next start
                        ''',
                        package_name
                    )

                continue

            # Finish reinstalling packages that could not be upgraded due to in-use files
            reinstall = os.path.join(package_dir, 'package-control.reinstall')
            if os.path.exists(reinstall):
                if clear_directory(package_dir) and self.manager.install_package(package_name):
                    console_write(
                        '''
                        Re-installed package %s
                        ''',
                        package_name
                    )

                else:
                    create_empty_file(reinstall)
                    console_write(
                        '''
                        Unable to re-install package %s -
                        deferring until next start
                        ''',
                        package_name
                    )

            found_packages.add(package_name)

        # Cleanup packages that were installed via Package Control, but we removed
        # from the "installed_packages" list - usually by removing them from another
        # computer and the settings file being synced.
        removed_packages = self.remove_orphaned_packages(found_packages - installed_packages_at_start)
        found_packages -= removed_packages

        invalid_packages = []

        # Check metadata to verify packages were not improperly installed
        for package in found_packages:
            metadata = self.manager.get_metadata(package)
            if metadata and not self.is_compatible(metadata):
                invalid_packages.append(package)

        if invalid_packages:
            def show_sync_error():
                message = ''
                if invalid_packages:
                    package_s = 's were' if len(invalid_packages) != 1 else ' was'
                    message += text.format(
                        '''
                        The following incompatible package%s found installed:

                        %s

                        ''',
                        (package_s, '\n'.join(invalid_packages))
                    )
                message += text.format(
                    '''
                    This is usually due to syncing packages across different
                    machines in a way that does not check package metadata for
                    compatibility.

                    Please visit https://packagecontrol.io/docs/syncing for
                    information about how to properly sync configuration and
                    packages across machines.

                    To restore package functionality, please remove each listed
                    package and reinstall it.
                    '''
                )
                show_error(message)
            sublime.set_timeout(show_sync_error, 100)

        sublime.set_timeout(lambda: self.finish(installed_packages, found_packages, found_libraries), 10)

    def remove_orphaned_packages(self, orphaned_packages):
        """
        Removes orphaned packages.

        The method removes all found and managed packages from filesystem,
        which are not present in `installed_packages`. They are considered
        active and therefore are disabled via PackageDisabler to properly
        reset theme/color scheme/syntax settings if needed.

        Compared to normal ``PackageManager.remove_package()` it doesn't
        - update `installed_packages` (not required)
        - remove orphaned libraries (will be done later)
        - send usage stats

        :param orphaned_packages:
            A set of orphened package names

        :returns:
            A set of orphaned packages, which have successfully been removed.
        """

        if not self.remove_orphaned:
            return set()

        # find all managed orphaned packages
        orphaned_packages = set(filter(
            lambda p: package_file_exists(p, 'package-metadata.json'),
            orphaned_packages
        ))

        if not orphaned_packages:
            return set()

        console_write(
            'Removing %d orphaned package%s...',
            (len(orphaned_packages), 's' if len(orphaned_packages) != 1 else '')
        )

        # disable orphaned packages and reset theme, color scheme or syntaxes if needed
        reenable = PackageDisabler.disable_packages(orphaned_packages, 'remove')
        time.sleep(0.7)

        try:
            for package_name in orphaned_packages:
                cleanup_complete = True

                installed_package_path = get_installed_package_path(package_name)
                try:
                    os.remove(installed_package_path)
                    console_write(
                        '''
                        Removed orphaned package %s
                        ''',
                        package_name
                    )
                except FileNotFoundError:
                    pass
                except (OSError, IOError) as e:
                    console_write(
                        '''
                        Unable to remove orphaned package %s -
                        deferring until next start: %s
                        ''',
                        (package_name, e)
                    )
                    cleanup_complete = False

                package_dir = get_package_dir(package_name)
                can_delete_dir = os.path.exists(package_dir)
                if can_delete_dir:
                    self.manager.backup_package_dir(package_name)
                    if delete_directory(package_dir):
                        console_write(
                            '''
                            Removed directory for orphaned package %s
                            ''',
                            package_name
                        )

                    else:
                        create_empty_file(os.path.join(package_dir, 'package-control.cleanup'))
                        console_write(
                            '''
                            Unable to remove directory for orphaned package %s -
                            deferring until next start
                            ''',
                            package_name
                        )
                        cleanup_complete = False

                if not cleanup_complete:
                    reenable.remove(package_name)

        finally:
            if reenable:
                time.sleep(0.7)
                PackageDisabler.reenable_packages(reenable, 'remove')

        return orphaned_packages

    def is_compatible(self, metadata):
        """
        Detects if a package is compatible with the current Sublime Text install

        :param metadata:
            A dict from a metadata file

        :return:
            If the package is compatible
        """

        sublime_text = metadata.get('sublime_text')
        platforms = metadata.get('platforms', [])

        # This indicates the metadata is old, so we assume a match
        if not sublime_text and not platforms:
            return True

        return is_compatible_platform(platforms) and is_compatible_version(sublime_text)

    def finish(self, installed_packages, found_packages, found_libraries):
        """
        A callback that can be run the main UI thread to perform saving of the
        Package Control.sublime-settings file. Also fires off the
        :class:`AutomaticUpgrader`.

        :param installed_packages:
            A list of the string package names of all "installed" packages,
            even ones that do not appear to be in the filesystem.

        :param found_packages:
            A list of the string package names of all packages that are
            currently installed on the filesystem.

        :param found_libraries:
            A list of library.Library() objects of all libraries that are
            currently installed on the filesystem.
        """

        # Make sure we didn't accidentally ignore packages because something
        # was interrupted before it completed.
        pc_filename = pc_settings_filename()
        pc_settings = sublime.load_settings(pc_filename)

        in_process = load_list_setting(pc_settings, 'in_process_packages')
        if in_process:
            filename = preferences_filename()
            settings = sublime.load_settings(filename)

            ignored = load_list_setting(settings, 'ignored_packages')
            new_ignored = list(ignored)
            for package in in_process:
                if package in new_ignored:
                    console_write(
                        '''
                        The package %s is being re-enabled after a Package
                        Control operation was interrupted
                        ''',
                        package
                    )
                    new_ignored.remove(package)

            save_list_setting(settings, filename, 'ignored_packages', new_ignored, ignored)
            save_list_setting(pc_settings, pc_filename, 'in_process_packages', [])

        save_list_setting(
            pc_settings,
            pc_filename,
            'installed_packages',
            installed_packages,
            self.original_installed_packages
        )
        AutomaticUpgrader(found_packages, found_libraries).start()
