#!/usr/bin/python3

"""Licensed under GPLv3, see https://www.gnu.org/licenses/"""

import atexit
import io
import os
import readline
import shutil
import signal
import sys
import traceback
from argparse import ArgumentError
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, Callable

import pyalpm

from .args import parse_args
from .config import (
    _OLD_AUR_REPOS_CACHE_PATH,
    AUR_REPOS_CACHE_PATH,
    CACHE_ROOT,
    CONFIG_PATH,
    DATA_ROOT,
    PikaurConfig,
)
from .core import (
    DEFAULT_INPUT_ENCODING,
    check_runtime_deps,
    interactive_spawn,
    isolate_root_cmd,
    run_with_sudo_loop,
    running_as_root,
    spawn,
    sudo,
)
from .exceptions import SysExit
from .getpkgbuild_cli import cli_getpkgbuild
from .help_cli import cli_print_help
from .i18n import translate
from .info_cli import cli_info_packages
from .install_cli import InstallPackagesCLI
from .pikspect import PikspectSignalHandler, TTYRestore
from .pkg_cache_cli import cli_clean_packages_cache
from .pprint import create_debug_logger, print_error, print_stderr, print_warning
from .print_department import print_version
from .prompt import NotANumberInputError, get_multiple_numbers_input
from .search_cli import cli_search_packages, search_packages
from .updates import print_upgradeable
from .urllib import ProxyInitSocks5Error, init_proxy


def init_readline() -> None:
    # follow GNU readline config in prompts:
    system_inputrc_path = '/etc/inputrc'
    if os.path.exists(system_inputrc_path):
        readline.read_init_file(system_inputrc_path)
    user_inputrc_path = os.path.expanduser('~/.inputrc')
    if os.path.exists(user_inputrc_path):
        readline.read_init_file(user_inputrc_path)


init_readline()

_debug = create_debug_logger('main')


class OutputEncodingWrapper(AbstractContextManager[None]):

    original_stderr: int
    original_stdout: int

    def __enter__(self) -> None:
        for attr in ('stdout', 'stderr'):
            _debug(f'Setting {attr} to {DEFAULT_INPUT_ENCODING}...', lock=False)
            real_stream = getattr(sys, attr)
            if real_stream.encoding == DEFAULT_INPUT_ENCODING:
                _debug('already set - nothing to do', lock=False)
                continue
            real_stream.flush()
            try:
                setattr(
                    self, f'original_{attr}',
                    real_stream
                )
                setattr(
                    sys, attr,
                    io.open(
                        real_stream.fileno(),
                        mode='w',
                        encoding=DEFAULT_INPUT_ENCODING,
                        closefd=False,
                    )
                )
            except io.UnsupportedOperation as exc:
                _debug(f"Can't set {attr} to {DEFAULT_INPUT_ENCODING}:\n{exc}", lock=False)

    def __exit__(
            self,
            exc_class: type | None, exc_instance: BaseException | None, exc_tb: TracebackType | None
    ) -> None:
        try:
            # @TODO: replace all SysExit-s to SystemExit-s eventually :3
            if exc_instance and exc_class and (exc_class not in (SysExit, SystemExit, )):
                # handling exception in context manager's __exit__ is not recommended
                # but otherwise stderr would be closed before exception is printed...
                if exc_tb:
                    print_stderr(''.join(traceback.format_tb(exc_tb)), lock=False)
                print_stderr(f"{exc_class.__name__}: {exc_instance}", lock=False)
                sys.exit(121)
        finally:
            for attr in ('stdout', 'stderr'):
                _debug(f'Restoring {attr}...', lock=False)
                stream = getattr(sys, attr)
                orig_stream = getattr(self, f'original_{attr}', None)
                if orig_stream in (None, stream, ):
                    _debug('nothing to do', lock=False)
                    continue
                stream.flush()
                setattr(
                    sys, attr,
                    orig_stream
                )
                _debug(f'{attr} restored', lock=False)
                stream.close()
                _debug(f'closed old {attr} stream', lock=False)


def cli_print_upgradeable() -> None:
    print_upgradeable()


def cli_install_packages() -> None:
    InstallPackagesCLI()


def cli_pkgbuild() -> None:
    cli_install_packages()


def cli_print_version() -> None:
    args = parse_args()
    proc = spawn(
        [PikaurConfig().misc.PacmanPath.get_str(), '--version', ],
    )
    if proc.stdout_text:
        pacman_version = proc.stdout_text.splitlines()[1].strip(' .-')
    else:
        pacman_version = "N/A"
    print_version(
        pacman_version=pacman_version, pyalpm_version=pyalpm.version(),
        quiet=args.quiet
    )


def cli_dynamic_select() -> None:  # pragma: no cover
    packages = search_packages(enumerated=True)
    if not packages:
        raise SysExit(1)

    while True:
        try:
            print_stderr(
                '\n' + translate(
                    "Please enter the number of the package(s) you want to install "
                    "and press [Enter] (default={}):"
                ).format(1)
            )
            answers = get_multiple_numbers_input('> ', list(range(1, len(packages) + 1))) or [1]
            print_stderr()
            selected_pkgs_idx = [idx - 1 for idx in answers]
            restart_prompt = False
            for idx in selected_pkgs_idx:
                if not 0 <= idx < len(packages):
                    print_error(translate('invalid value: {} is not between {} and {}').format(
                        idx + 1, 1, len(packages) + 1
                    ))
                    restart_prompt = True
            if restart_prompt:
                continue
            break
        except NotANumberInputError as exc:
            if exc.character.lower() == translate('n'):
                raise SysExit(128) from exc
            print_error(translate('invalid number: {}').format(exc.character))

    parse_args().positional = [packages[idx].name for idx in selected_pkgs_idx]
    cli_install_packages()


def cli_entry_point() -> None:  # pylint: disable=too-many-statements
    # pylint: disable=too-many-branches

    try:
        init_proxy()
    except ProxyInitSocks5Error as exc:
        print_error(''.join(exc.args))
        sys.exit(2)

    # operations are parsed in order what the less destructive (like info and query)
    # are being handled first, for cases when user by mistake
    # specified both operations, like `pikaur -QS smth`

    args = parse_args()
    pikaur_operation: Callable[[], None] | None = None
    require_sudo = False

    if args.help:
        pikaur_operation = cli_print_help

    elif args.version:
        pikaur_operation = cli_print_version

    elif args.query:
        if args.sysupgrade:
            pikaur_operation = cli_print_upgradeable

    elif args.files:
        require_sudo = bool(args.refresh)

    elif args.getpkgbuild:
        pikaur_operation = cli_getpkgbuild

    elif args.pkgbuild:
        require_sudo = True
        pikaur_operation = cli_pkgbuild

    elif args.sync:
        if args.search:
            pikaur_operation = cli_search_packages
        elif args.info:
            pikaur_operation = cli_info_packages
        elif args.clean:
            if not args.aur:
                require_sudo = True
            pikaur_operation = cli_clean_packages_cache
        elif args.groups or args.list:   # @TODO: implement -l/--list
            require_sudo = False
        else:
            require_sudo = True
            pikaur_operation = cli_install_packages

    elif not (args.database or args.remove or args.deptest or args.upgrade):
        if args.positional:
            pikaur_operation = cli_dynamic_select

    else:
        require_sudo = True

    if pikaur_operation:
        _debug(f"Pikaur operation found for {sys.argv=}: {pikaur_operation.__name__}")
        if require_sudo and args.dynamic_users and not running_as_root():
            # Restart pikaur with sudo to use systemd dynamic users
            restart_args = sys.argv[:]
            config_overridden = max(
                arg.startswith('--pikaur-config')
                for arg in restart_args
            )
            if not config_overridden:
                restart_args += ['--pikaur-config', CONFIG_PATH]
            sys.exit(interactive_spawn(
                sudo(restart_args)
            ).returncode)
        else:
            if not require_sudo or running_as_root():
                # Just run the operation normally
                pikaur_operation()
            else:
                # Or use sudo loop if not running as root but need to have it later
                run_with_sudo_loop(pikaur_operation)
    else:
        # Just bypass all the args to pacman
        _debug(f"Pikaur operation not found for {sys.argv=}")
        _debug(args)
        pacman_args = [
            PikaurConfig().misc.PacmanPath.get_str(),
        ] + args.raw_without_pikaur_specific
        if require_sudo:
            pacman_args = sudo(pacman_args)
        sys.exit(
            interactive_spawn(pacman_args).returncode
        )


def migrate_old_aur_repos_dir() -> None:
    if not (
            os.path.exists(_OLD_AUR_REPOS_CACHE_PATH) and not os.path.exists(AUR_REPOS_CACHE_PATH)
    ):
        return
    if not os.path.exists(DATA_ROOT):
        os.makedirs(DATA_ROOT)
    shutil.move(_OLD_AUR_REPOS_CACHE_PATH, AUR_REPOS_CACHE_PATH)

    print_stderr()
    print_warning(
        translate(
            "AUR repos dir has been moved from '{old}' to '{new}'."
        ).format(
            old=_OLD_AUR_REPOS_CACHE_PATH,
            new=AUR_REPOS_CACHE_PATH
        )
    )
    print_stderr()


def create_dirs() -> None:
    if running_as_root():
        # Let systemd-run setup the directories and symlinks
        true_cmd = isolate_root_cmd(['true'])
        result = spawn(true_cmd)
        if result.returncode != 0:
            raise Exception(result)
        # Chown the private CacheDirectory to root to signal systemd that
        # it needs to recursively chown it to the correct user
        os.chown(os.path.realpath(CACHE_ROOT), 0, 0)
    if not os.path.exists(CACHE_ROOT):
        os.makedirs(CACHE_ROOT)
    migrate_old_aur_repos_dir()
    if not os.path.exists(AUR_REPOS_CACHE_PATH):
        os.makedirs(AUR_REPOS_CACHE_PATH)


def restore_tty() -> None:
    TTYRestore.restore()


def handle_sig_int(*_whatever: Any) -> None:  # pragma: no cover
    if signal_handler := PikspectSignalHandler.get():
        signal_handler(*_whatever)  # pylint: disable=not-callable
        return
    if parse_args().pikaur_debug:
        raise KeyboardInterrupt()
    print_stderr("\n\nCanceled by user (SIGINT)", lock=False)
    raise SysExit(125)


class EmptyWrapper:

    def __enter__(self) -> None:
        pass

    def __exit__(self, *_exc_details: Any) -> None:
        pass


def main(embed: bool = False) -> None:
    wrapper: type[AbstractContextManager[None]] = OutputEncodingWrapper
    if embed:
        wrapper = EmptyWrapper
    with wrapper():
        try:
            parse_args()
        except ArgumentError as exc:
            print_stderr(exc)
            sys.exit(22)
        check_runtime_deps()

        create_dirs()
        # initialize config to avoid race condition in threads:
        PikaurConfig.get_config()

        atexit.register(restore_tty)
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        signal.signal(signal.SIGINT, handle_sig_int)

        try:
            cli_entry_point()
        except BrokenPipeError:
            # @TODO: should it be 32?
            sys.exit(0)
        except SysExit as exc:
            sys.exit(exc.code)
        sys.exit(0)


if __name__ == '__main__':
    main()
