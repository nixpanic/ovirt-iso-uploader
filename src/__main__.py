#!/usr/bin/python

# Copyright 2011-2016 Red Hat, Inc. and/or its affiliates.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import sys
import os
from optparse import OptionParser, OptionGroup, SUPPRESS_HELP
import subprocess
import shlex
import logging
import gettext
import traceback
import tempfile
import time
import shutil
from pwd import getpwnam
import getpass
import ovirtsdk4
from ovirt_iso_uploader import config

APP_NAME = "ovirt-iso-uploader"
VERSION = "4.1.0"
DEFAULT_IMAGES_DIR = 'images/11111111-1111-1111-1111-111111111111'
NFS_MOUNT_OPTS = '-t nfs -o rw,sync,soft'
NFS_UMOUNT_OPTS = '-t nfs -f '
NFS_USER = 'vdsm'
NUMERIC_VDSM_ID = 36
SUDO = '/usr/bin/sudo'
MOUNT = '/bin/mount'
UMOUNT = '/bin/umount'
SSH = '/usr/bin/ssh'
SCP = '/usr/bin/scp'
CP = '/bin/cp'
RM = '/bin/rm -fv'
MV = '/bin/mv -fv'
CHOWN = '/bin/chown'
CHMOD = '/bin/chmod'
TEST = '/usr/bin/test'
DEFAULT_CONFIGURATION_FILE = '/etc/ovirt-engine/isouploader.conf'
PERMS_MASK = '640'
PYTHON = '/usr/bin/python'

# {Logging system
STREAM_LOG_FORMAT = '%(levelname)s: %(message)s'
FILE_LOG_FORMAT = (
    '%(asctime)s::'
    '%(levelname)s::'
    '%(module)s::'
    '%(lineno)d::'
    '%(name)s::'
    ' %(message)s'
)
FILE_LOG_DSTMP = '%Y-%m-%d %H:%M:%S'
DEFAULT_LOG_FILE = os.path.join(
    config.DEFAULT_LOG_DIR,
    '{prefix}-{timestamp}.log'.format(
        prefix=config.LOG_PREFIX,
        timestamp=time.strftime('%Y%m%d%H%M%S'),
    )
)


class NotAnError(logging.Filter):

    def filter(self, entry):
        return entry.levelno < logging.ERROR


def multilog(logger, msg):
    for line in str(msg).splitlines():
        logger(line)
# }


def get_from_prompt(msg, default=None, prompter=raw_input):
    try:
        return prompter(msg)
    except EOFError:
        print
        return default


class ExitCodes():
    """
    A simple psudo-enumeration class to hold the current and future exit codes
    """
    NOERR = 0
    CRITICAL = 1
    LIST_ISO_ERR = 2
    UPLOAD_ERR = 3
    CLEANUP_ERR = 4
    exit_code = NOERR


class Commands():
    """
    A simple psudo-enumeration class to facilitate command checking.
    """
    LIST = 'list'
    UPLOAD = 'upload'
    # DELETE = 'delete'
    ARY = [LIST, UPLOAD]


class NEISODomain(RuntimeError):
    """"
    This exception is raised when the user inputs a not existing ISO domain
    """
    pass


class Caller(object):
    """
    Utility class for forking programs.
    """

    def __init__(self, configuration):
        self.configuration = configuration

    def prep(self, cmd):
        _cmd = cmd % self.configuration
        logging.debug(_cmd)
        return shlex.split(_cmd)

    def call(self, cmds):
        """
        Uses the configuration to fork a subprocess and run cmds
        """
        _cmds = self.prep(cmds)
        logging.debug("_cmds(%s)" % _cmds)
        proc = subprocess.Popen(
            _cmds,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = proc.communicate()
        returncode = proc.returncode
        logging.debug("returncode(%s)" % returncode)
        logging.debug("STDOUT(%s)" % stdout)
        logging.debug("STDERR(%s)" % stderr)

        if returncode == 0:
            return (stdout, returncode)
        else:
            raise Exception(stderr)


class Configuration(dict):
    """
    This class is a dictionary subclass that knows how to read and
    handle our configuration. Resolution order is defaults ->
    configuration file -> command line options.
    """

    class SkipException(Exception):
        "This exception is raised when the user aborts a prompt"
        pass

    def __init__(self,
                 parser=None):
        self.command = None
        self.parser = parser
        self.options = None
        self.args = None
        self.files = []

        # Immediately, initialize the logger to the INFO log level and our
        # logging format which is <LEVEL>: <MSG> and not the default of
        # <LEVEL>:<UID: <MSG>
        self.__initLogger(logging.INFO)

        if not parser:
            raise Exception("Configuration requires a parser")

        self.options, self.args = self.parser.parse_args()

        if os.geteuid() != 0:
            raise Exception("This tool requires root permissions to run.")

        # At this point we know enough about the command line options
        # to test for verbose and if it is set we should re-initialize
        # the logger to DEBUG.  This will have the effect of printing
        # stack traces if there are any exceptions in this class.
        if getattr(self.options, "verbose"):
            self.__initLogger(logging.DEBUG)

        self.load_config_file()

        if self.options:
            # Need to parse again to override configuration file options
            self.options, self.args = self.parser.parse_args(
                values=self.options
            )
            self.from_options(self.options, self.parser)
            # Need to parse out options from the option groups.
            self.from_option_groups(self.options, self.parser)

        if self.args:
            self.from_args(self.args)

        # Finally, all options from the command line and possibly
        # a configuration file have been processed.  We need to
        # re-initialize the logger if
        # the user has supplied either --quiet processing or
        # supplied a --log-file.
        # This will ensure that any further log messages
        # throughout the lifecycle
        # of this program go to the log handlers that the
        # user has specified.
        if self.options.log_file or self.options.quiet:
            level = logging.INFO
            if self.options.verbose:
                level = logging.DEBUG
            self.__initLogger(level, self.options.quiet, self.options.log_file)

    def __missing__(self, key):
        return None

    def load_config_file(self):
        """Loads the user-supplied config file or the system default.
           If the user supplies a bad filename we will stop."""

        conf_file = DEFAULT_CONFIGURATION_FILE

        if self.options and getattr(self.options, "conf_file"):
            conf_file = self.options.conf_file
            if (
                not os.path.exists(conf_file) and
                not os.path.exists("%s.d" % conf_file)
            ):
                raise Exception(
                    (
                        "The specified configuration file "
                        "does not exist.  File=(%s)"
                    ) % self.options.conf_file
                )

        self.from_file(conf_file)

    def from_option_groups(self, options, parser):
        for optGrp in parser.option_groups:
            for optGrpOpts in optGrp.option_list:
                opt_value = getattr(options, optGrpOpts.dest)
                if opt_value is not None:
                    self[optGrpOpts.dest] = opt_value

    def from_options(self, options, parser):
        for option in parser.option_list:
            if option.dest:
                opt_value = getattr(options, option.dest)
                if opt_value is not None:
                    self[option.dest] = opt_value

    def from_file(self, configFile):
        import ConfigParser
        import glob

        configs = []
        configDir = '%s.d' % configFile
        if os.path.exists(configFile):
            configs.append(configFile)
        configs += sorted(
            glob.glob(
                os.path.join(configDir, "*.conf")
            )
        )

        cp = ConfigParser.ConfigParser()
        cp.read(configs)

        # backward compatibility with existing setup
        if cp.has_option('ISOUploader', 'rhevm'):
            if not cp.has_option('ISOUploader', 'engine'):
                cp.set(
                    'ISOUploader',
                    'engine',
                    cp.get('ISOUploader', 'rhevm')
                )
            cp.remove_option('ISOUploader', 'rhevm')
        if cp.has_option('ISOUploader', 'engine-ca'):
            if not cp.has_option('ISOUploader', 'cert-file'):
                cp.set(
                    'ISOUploader',
                    'cert-file',
                    cp.get('ISOUploader', 'engine-ca')
                )
            cp.remove_option('ISOUploader', 'engine-ca')

        # we want the items from the ISOUploader section only
        try:
            opts = [
                "--%s=%s" % (k, v)
                for k, v in cp.items("ISOUploader")
            ]
            (new_options, args) = self.parser.parse_args(
                args=opts,
                values=self.options
            )
            self.from_option_groups(new_options, self.parser)
            self.from_options(new_options, self.parser)
        except ConfigParser.NoSectionError:
            pass

    def from_args(self, args):
        self.command = args[0]
        if self.command not in Commands.ARY:
            raise Exception(
                _(
                    "%s is not a valid command.  Valid commands "
                    "are '%s' or '%s'."
                ) % (
                    self.command,
                    Commands.LIST,
                    Commands.UPLOAD
                )
            )

        if self.command == Commands.UPLOAD:
            if len(args) <= 1:
                raise Exception(_("Files must be supplied for %s commands" %
                                  (Commands.UPLOAD)))
            for file in args[1:]:
                self.files.append(file)

    def prompt(self, key, msg):
        if key not in self:
            self._prompt(raw_input, key, msg)

    def getpass(self, key, msg):
        if key not in self:
            self._prompt(getpass.getpass, key, msg)

    # This doesn't ask for CTRL+C to abort because KeyboardInterrupts don't
    # seem to behave the same way every time. Take a look at the link:
    # http://stackoverflow.com/questions/4606942/
    #   why-cant-i-handle-a-keyboardinterrupt-in-python
    def _prompt(self, prompt_function, key, msg=None):
        value = get_from_prompt(
            msg="Please provide the %s (CTRL+D to abort): " % msg,
            prompter=prompt_function
        )
        if value:
            self[key] = value
        else:
            raise self.SkipException

    def ensure(self, key, default=""):
        if key not in self:
            self[key] = default

    def has_all(self, *keys):
        return all(self.get(key) for key in keys)

    def has_any(self, *keys):
        return any(self.get(key) for key in keys)

    def __ensure_path_to_file(self, file_):
        dir_ = os.path.dirname(file_)
        if not os.path.exists(dir_):
            logging.info("%s does not exists. It will be created." % dir_)
            os.makedirs(dir_, 0755)

    def __log_to_file(self, file_, level):
        try:
            self.__ensure_path_to_file(file_)
            hdlr = logging.FileHandler(filename=file_, mode='w')
            fmt = logging.Formatter(FILE_LOG_FORMAT, FILE_LOG_DSTMP)
            hdlr.setFormatter(fmt)
            logging.root.addHandler(hdlr)
            logging.root.setLevel(level)
        except Exception, e:
            logging.error("Could not configure file logging: %s" % e)

    def __log_to_stream(self, level):
        fmt = logging.Formatter(STREAM_LOG_FORMAT)
        # Errors should always be there, on stderr
        h_err = logging.StreamHandler(sys.stderr)
        h_err.setLevel(logging.ERROR)
        h_err.setFormatter(fmt)
        logging.root.addHandler(h_err)
        # Other logs should go to stdout
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(level)
        sh.setFormatter(fmt)
        sh.addFilter(NotAnError())
        logging.root.addHandler(sh)

    def __initLogger(self, logLevel=logging.INFO, quiet=None, logFile=None):
        """
        Initialize the logger based on information supplied from the
        command line or configuration file.
        """
        # If you call basicConfig more than once without removing handlers
        # it is effectively a noop. In this program it is possible to call
        # __initLogger more than once as we learn information about what
        # options the user has supplied in either the config file or
        # command line; hence, we will need to load and unload the handlers
        # to ensure consistently fomatted output.
        log = logging.getLogger()
        for h in list(log.handlers):
            log.removeHandler(h)

        if quiet:
            if logFile:
                # Case: Quiet and log file supplied.  Log to only file
                self.__log_to_file(logFile, logLevel)
            else:
                # If the user elected quiet mode *and* did not supply
                # a file.  We will be *mostly* quiet but not completely.
                # If there is an exception/error/critical we will print
                # to stdout/stderr.
                logging.basicConfig(
                    level=logging.ERROR,
                    format=STREAM_LOG_FORMAT
                )
        else:
            if logFile:
                # Case: Not quiet and log file supplied.
                # Log to both file and stdout/stderr
                self.__log_to_file(logFile, logLevel)
                self.__log_to_stream(logLevel)
            else:
                # Case: Not quiet and no log file supplied.
                # Log to only stdout/stderr
                self.__log_to_stream(logLevel)


class ISOUploader(object):

    def __init__(self, conf):
        self.api = None
        self.configuration = conf
        self.caller = Caller(self.configuration)
        if self.configuration.command == Commands.LIST:
            self.list_all_ISO_storage_domains()
        elif self.configuration.command == Commands.UPLOAD:
            self.upload_to_storage_domain()
        else:
            raise Exception(_("A valid command was not specified."))

    def _initialize_api(self):
        """
        Make a RESTful request to the supplied oVirt Engine method.
        """
        if not self.configuration:
            raise Exception("No configuration.")

        with_kerberos = bool(self.configuration.get("kerberos"))

        if self.api is None:
            # The API has not been initialized yet.
            try:
                self.configuration.prompt(
                    "engine",
                    msg=_("hostname of oVirt Engine")
                )
                if not with_kerberos:
                    self.configuration.prompt(
                        "user",
                        msg=_("REST API username for oVirt Engine")
                    )
                    self.configuration.getpass(
                        "passwd",
                        msg=(
                            _(
                                "REST API password for the %s oVirt "
                                "Engine user"
                            ) % self.configuration.get("user")
                        )
                    )
            except Configuration.SkipException:
                raise Exception(
                    "Insufficient information provided to communicate with "
                    "the oVirt Engine REST API."
                )

            url = "https://{engine}/ovirt-engine/api".format(
                engine=self.configuration.get("engine"),
            )

            try:
                self.api = ovirtsdk4.Connection(
                    url=url,
                    username=self.configuration.get("user"),
                    password=self.configuration.get("passwd"),
                    ca_file=self.configuration.get("cert_file"),
                    insecure=bool(self.configuration.get("insecure")),
                    kerberos=with_kerberos,
                )
                svc = self.api.system_service().get()
                pi = svc.product_info
                if pi is not None:
                    vrm = '%s.%s.%s' % (
                        pi.version.major,
                        pi.version.minor,
                        pi.version.revision,
                    )
                    logging.debug(
                        "API Vendor(%s)\tAPI Version(%s)",
                        pi.vendor,
                        vrm
                    )
                else:
                    logging.error(
                        _("Unable to connect to REST API at {url}").format(
                            url=url,
                        )
                    )
                    return False
            except ovirtsdk4.Error as e:
                # this is the only exception raised by SDK :(
                logging.error(
                    _(
                        "Unable to connect to REST API at {url} due to SDK "
                        "error\nMessage: {e}"
                    ).format(
                        url=url,
                        e=e,
                    ),
                )
                return False
            except Exception as e:
                logging.error(
                    _(
                        "Unable to connect to REST API at {url}\n"
                        "Message: {e}"
                    ).format(
                        url=url,
                        e=e,
                    )
                )
                return False
        return True

    def list_all_ISO_storage_domains(self):
        """
        List only the ISO storage domains in sorted format.
        """
        def get_name(ary):
            return ary[0]

        if not self._initialize_api():
            sys.exit(ExitCodes.CRITICAL)

        svc = self.api.system_service()
        domainAry = svc.storage_domains_service().list()
        if domainAry is not None:
            isoAry = []
            for domain in domainAry:
                if domain.type.value == 'iso':
                    status = domain.external_status
                    if status is not None:
                        isoAry.append(
                            [
                                domain.name,
                                status.value
                            ]
                        )
                    else:
                        logging.debug(
                            "the storage domain didn't "
                            "have a status element."
                        )
            if len(isoAry) > 0:
                isoAry.sort(key=get_name)
                fmt = "%-25s | %s"
                print fmt % (
                    _("ISO Storage Domain Name"),
                    _("ISO Domain Status")
                )
                print "\n".join(
                    fmt % (name, status)
                    for name, status in isoAry
                )
            else:
                ExitCodes.exit_code = ExitCodes.LIST_ISO_ERR
                logging.error(_("There are no ISO storage domains."))
        else:
            ExitCodes.exit_code = ExitCodes.LIST_ISO_ERR
            logging.error(
                _("There are no storage domains available.")
            )

    def get_host_and_path_from_ISO_domain(self, isodomain):
        """
        Given a valid ISO storage domain, this method will return the
        hostname/IP, UUID, and path to the domain in a 3 tuple.
        Returns:
          (host, id, path)
        """
        if not self._initialize_api():
            sys.exit(ExitCodes.CRITICAL)
        svc = self.api.system_service()
        sd = None
        for domain in svc.storage_domains_service().list():
            if domain.name == isodomain:
                sd = domain
        if sd is not None:
            if sd.type.value != 'iso':
                raise Exception(
                    _("The %s storage domain supplied is not of type ISO") %
                    isodomain
                )
            sd_uuid = sd.id
            storage = sd.storage
            if storage is not None:
                domain_type = storage.type.value
                address = ''
                if domain_type == 'localfs':
                    hosts = svc.hosts_service().list(
                        search="storage=%s" % isodomain
                    )
                    for host in hosts:
                        address = host.address
                else:
                    address = storage.address
                path = storage.path
                if len(address) == 0:
                    raise Exception(
                        _(
                            "An host was not found for "
                            "the %s local ISO domain."
                        ) % isodomain
                    )
            else:
                raise Exception(
                    _(
                        "A storage element was not found for "
                        "the %s ISO domain."
                    ) % isodomain
                )
            logging.debug(
                'id=%s address=%s path=%s' % (sd_uuid, address, path)
            )
            return (sd_uuid, domain_type, address, path)
        else:
            raise NEISODomain(
                _("An ISO storage domain with a name of %s was not found.") %
                isodomain
            )

    def format_ssh_user(self, ssh_user):
        if ssh_user and not ssh_user.endswith("@"):
            return "%s@" % ssh_user
        else:
            return ssh_user or ""

    def format_ssh_command(self, cmd=SSH):
        cmd = "%s " % cmd
        port_flag = "-p" if cmd.startswith(SSH) else "-P"
        if "ssh_port" in self.configuration:
            cmd += port_flag + " %(ssh_port)s " % self.configuration
        if "key_file" in self.configuration:
            cmd += "-i %(key_file)s " % self.configuration
        return cmd

    def format_nfs_command(self, address, export, dir):
        cmd = '%s %s %s:%s %s' % (MOUNT, NFS_MOUNT_OPTS, address, export, dir)
        logging.debug('NFS mount command (%s)' % cmd)
        return cmd

    def exists_nfs(self, file, uid, gid):
        """
        Check for file existence.  The file will be tested as the
        UID and GID provided which is important for NFS.
        """
        try:
            os.setegid(gid)
            os.seteuid(uid)
            return os.path.exists(file)
        except Exception:
            raise Exception("unable to test the available space on %s" % dir)
        finally:
            os.seteuid(0)
            os.setegid(0)

    def exists_ssh(self, user, address, file):
        """
        Given a ssh user, ssh server, and full path to a file on the
        SSH server this command will test to see if it exists on the
        target file server and return true if it does.  False otherwise.
        """

        cmd = self.format_ssh_command()
        cmd += ' %s%s "%s -e %s"' % (user, address, TEST, file)
        logging.debug(cmd)
        returncode = 1
        try:
            stdout, returncode = self.caller.call(cmd)
        except:
            pass

        if returncode == 0:
            logging.debug("exists returning true")
            return True
        else:
            logging.debug("exists returning false")
            return False

    def space_test_ssh(self, user, address, dir, file):
        """
        Function to test if the given file will fit on the given
        remote directory.  This function will return the available
        space in bytes of dir and the size of file.
        """
        dir_size = None
        returncode = 1
        cmd = self.format_ssh_command()
        cmd += (
            """ %s%s "%s -c 'import os; dir_stat = os.statvfs(\\"%s\\"); """
            """print (dir_stat.f_bavail * dir_stat.f_frsize)'" """
        ) % (user, address, PYTHON, dir)
        logging.debug('Mount point size test command is (%s)' % cmd)
        try:
            dir_size, returncode = self.caller.call(cmd)
        except Exception:
            pass

        if returncode == 0 and dir_size is not None:
            # This simply means that the SSH command was successful.
            dir_size = dir_size.strip()
            file_size = os.path.getsize(file)
            logging.debug(
                "Size of %s:\t%s bytes\t%.1f 1K-blocks\t%.1f MB",
                file, file_size, file_size / 1024, (file_size / 1024) / 1024
            )
            logging.debug(
                "Available space in %s:\t%s bytes\t%.1f 1K-blocks\t%.1f MB",
                dir, dir_size, float(dir_size) / 1024,
                (float(dir_size) / 1024) / 1024
            )
            return (dir_size, file_size)
        else:
            raise Exception("unable to test the available space on %s" % dir)

    def space_test_nfs(self, dir, file, uid, gid):
        """
        Checks to see if there is enough space in dir for file.
        This function will return the available
        space in bytes of dir and the size of file.
        """
        try:
            os.setegid(gid)
            os.seteuid(uid)
            dir_stat = os.statvfs(dir)
        except Exception:
            raise Exception(
                "unable to test the available space on %s" % dir
            )
        finally:
            os.seteuid(0)
            os.setegid(0)

        dir_size = (dir_stat.f_bavail * dir_stat.f_frsize)
        file_size = os.path.getsize(file)
        logging.debug(
            "Size of %s:\t%s bytes\t%.1f 1K-blocks\t%.1f MB",
            file, file_size, file_size / 1024, (file_size / 1024) / 1024
        )
        logging.debug(
            "Available space in %s:\t%s bytes\t%.1f 1K-blocks\t%.1f MB",
            dir, dir_size, dir_size / 1024, (dir_size / 1024) / 1024
        )
        return (dir_size, file_size)

    def copyfileobj_sparse_progress(
            self,
            fsrc,
            fdst,
            length=16*1024,
            make_sparse=True,
            bar_length=40,
            quiet=True,
    ):
        """
        copy data from file-like object fsrc to file-like object fdst
        like shutils.copyfileobj does but supporting also
        sparse file. It can print also a progress bar
        """
        i = 0
        fsrc.seek(0, 2)  # move the cursor to the end of the file
        end_val = fsrc.tell()
        fsrc.seek(0, 0)  # move back the cursor to the start of the file
        old_ipercent = -1
        while 1:
            buf = fsrc.read(length)
            if not buf:
                break
            if make_sparse and buf == '\0'*len(buf):
                fdst.seek(len(buf), os.SEEK_CUR)
            else:
                fdst.write(buf)
            i += length
            percent = min(float(i) / end_val, 1.0)
            ipercent = int(round(percent * 100))
            if not quiet and ipercent > old_ipercent:
                old_ipercent = ipercent
                hashes = '#' * int(round(percent * bar_length))
                spaces = ' ' * (bar_length - len(hashes))
                sys.stdout.write(
                    _(
                        "\rUploading: [{h}] {n}%".format(
                            h=hashes + spaces,
                            n=ipercent,
                        )
                    )
                )
                sys.stdout.flush()
        if make_sparse:
            # Make sure the file ends where it should, even if padded out.
            fdst.truncate()
        if not quiet:
            sys.stdout.write('\n')
            sys.stdout.flush()

    def copy_file(self, src_file_name, dest_file_name, uid, gid):
        """
        Copy a file from source to dest via file handles.  The destination
        file will be opened and written to as the UID and GID provided.
        This odd copy operation is important when copying files over NFS.
        Read the NFS spec if you want to figure out *why* you need to do this.
        Returns: True if successful and false otherwise.
        """
        retVal = True
        logging.debug("euid(%s) egid(%s)" % (os.geteuid(), os.getegid()))
        umask_save = os.umask(0137)  # Set to 640
        try:
            src = open(src_file_name, 'r')
            os.setegid(gid)
            os.seteuid(uid)
            dest = open(dest_file_name, 'w')
            self.copyfileobj_sparse_progress(
                fsrc=src,
                fdst=dest,
                quiet=self.configuration.options.quiet,
            )
        except Exception, e:
            retVal = False
            logging.error(_("Problem copying %s to %s.  Message: %s" %
                          (src_file_name, dest_file_name, e)))
        finally:
            os.umask(umask_save)
            os.seteuid(0)
            os.setegid(0)
            src.close()
            dest.close()
        return retVal

    def rename_file_nfs(self, src_file_name, dest_file_name, uid, gid):
        """
        Rename a file from source to dest as the UID and GID provided.
        This method will set the euid and egid to that which is provided
        and then perform the rename.  This is can be important on an
        NFS mount.
        """
        logging.debug("euid(%s) egid(%s)" % (os.geteuid(), os.getegid()))
        umask_save = os.umask(0137)  # Set to 640
        try:
            os.setegid(gid)
            os.seteuid(uid)
            logging.debug(
                'Renaming {src} to {dest}'.format(
                    src=src_file_name,
                    dest=dest_file_name,
                )
            )
            os.rename(src_file_name, dest_file_name)
            success = True
        except Exception, e:
            success = False
            logging.error(
                _(
                    'Problem renaming {src} to {dst}. Message: {msg}'
                ).format(
                    src=src_file_name,
                    dst=dest_file_name,
                    msg=e,
                )
            )
            logging.error(
                _(
                    'Please ensure to have permissions for renaming files '
                    'inside {directory}'
                ).format(
                    directory=os.path.dirname(dest_file_name)
                )
            )
            ExitCodes.exit_code = ExitCodes.UPLOAD_ERR
        finally:
            os.seteuid(0)
            os.setegid(0)
            os.umask(umask_save)
        return success

    def rename_file_ssh(self, user, address, src_file_name, dest_file_name):
        """
        This method will remove a file via SSH.
        """
        cmd = self.format_ssh_command()
        cmd += """ %s%s "%s %s %s" """ % (
            user,
            address,
            MV,
            src_file_name,
            dest_file_name
        )
        logging.debug('Rename file command is (%s)' % cmd)
        try:
            stdout, returncode = self.caller.call(cmd)
        except Exception:
            raise Exception(
                "unable to move file from %s to %s" % (
                    src_file_name,
                    dest_file_name
                )
            )

    def remove_file_nfs(self, file_name, uid, gid):
        """
        Remove a file as the UID and GID provided.
        This method will set the euid and egid to that which is provided
        and then perform the remove.  This is can be important on an
        NFS mount.
        """
        logging.debug("euid(%s) egid(%s)" % (os.geteuid(), os.getegid()))
        try:
            os.setegid(gid)
            os.seteuid(uid)
            os.remove(file_name)
        except Exception, e:
            logging.error(_("Problem removing %s.  Message: %s" %
                          (file_name, e)))
        finally:
            os.seteuid(0)
            os.setegid(0)

    def remove_file_ssh(self, user, address, file):
        """
        This method will remove a file via SSH.
        """

        cmd = self.format_ssh_command()
        cmd += """ %s%s "%s %s" """ % (user, address, RM, file)
        logging.debug('Remove file command is (%s)' % cmd)
        try:
            stdout, returncode = self.caller.call(cmd)
        except Exception:
            raise Exception("unable to remove %s" % file)

    def refresh_iso_domain(self, id):
        """
        oVirt Engine scans and caches the list of files in each ISO domain.  It
        does this on a predefined interval.  Poking the
        /storagedomains/<id>/files
        RESTful method will cause it to refresh that list.
        """
        if not self._initialize_api():
            sys.exit(ExitCodes.CRITICAL)
        try:
            svc = self.api.system_service()
            sd = svc.storage_domains_service().service(id)
            if sd is not None:
                sd.files_service().list()
        except Exception, e:
            logging.warn(
                _(
                    "failed to refresh the list of files available in the "
                    "%s ISO storage domain. Please refresh the list manually "
                    "using the 'Refresh' button in the oVirt Webadmin "
                    "console."
                ),
                self.configuration.get('iso_domain')
            )
            logging.debug(e)

    def upload_to_storage_domain(self):
        """
        Method to upload a designated file to an ISO storage domain.
        """
        # TODO: refactor this method
        remote_path = ''
        id = None
        domain_type = None
        # Did the user give us enough info to do our work?
        if (
            self.configuration.get('iso_domain') and
            self.configuration.get('nfs_server')
        ):
            raise Exception(
                _("iso-domain and nfs-server are mutually exclusive options")
            )
        if (
            self.configuration.get('ssh_user') and
            self.configuration.get('nfs_server')
        ):
            raise Exception(
                _("ssh-user and nfs-server are mutually exclusive options")
            )
        elif self.configuration.get('iso_domain'):
            # Discover the hostname and path from the ISO domain.
            iso_domain_data = self.get_host_and_path_from_ISO_domain(
                self.configuration.get('iso_domain')
            )
            if iso_domain_data is None:
                raise Exception(
                    _('Unable to get ISO domain data')
                )
            (id, domain_type, address, path) = iso_domain_data
            remote_path = os.path.join(id, DEFAULT_IMAGES_DIR)
        elif self.configuration.get('nfs_server'):
            mnt = self.configuration.get('nfs_server')
            (address, sep, path) = mnt.partition(':')
        else:
            raise Exception(
                _("either iso-domain or nfs-server must be provided")
            )
        print _("Uploading, please wait...")
        # We need to create the full path to the images directory
        if conf.get('ssh_user'):
            for filename in self.configuration.files:
                logging.info(_("Start uploading %s "), filename)
                try:
                    logging.debug('file (%s)' % filename)
                    dest_dir = os.path.join(path, remote_path)
                    dest_file = os.path.join(
                        dest_dir,
                        os.path.basename(filename)
                    )
                    user = self.format_ssh_user(self.configuration["ssh_user"])
                    retVal = self.exists_ssh(user, address, dest_file)
                    if conf.get('force') or not retVal:
                        temp_dest_file = os.path.join(
                            dest_dir,
                            '.%s' % os.path.basename(filename)
                        )
                        if retVal:
                            self.remove_file_ssh(user, address, dest_file)
                        (dir_size, file_size) = self.space_test_ssh(
                            user,
                            address,
                            path,
                            filename
                        )
                        if (long(dir_size) > long(file_size)):
                            cmd = self.format_ssh_command(SCP)
                            cmd += ' %s %s%s:%s' % (
                                filename,
                                user,
                                address,
                                temp_dest_file
                            )
                            logging.debug('SCP command is (%s)' % cmd)
                            self.caller.call(cmd)
                            if (
                                self.format_ssh_user(
                                    self.configuration["ssh_user"]
                                ) == 'root@'
                            ):
                                cmd = self.format_ssh_command()
                                cmd += ' %s%s "%s %s:%s %s"' % (
                                    user,
                                    address,
                                    CHOWN,
                                    NUMERIC_VDSM_ID,
                                    NUMERIC_VDSM_ID,
                                    temp_dest_file
                                )
                                logging.debug('CHOWN command is (%s)', cmd)
                                self.caller.call(cmd)
                            # chmod the file to 640.  Do this for every
                            # user (i.e. root and otherwise)
                            cmd = self.format_ssh_command()
                            cmd += ' %s%s "%s %s %s"' % (
                                user,
                                address,
                                CHMOD,
                                PERMS_MASK,
                                temp_dest_file
                            )
                            logging.debug('CHMOD command is (%s)', cmd)
                            self.caller.call(cmd)
                            self.rename_file_ssh(
                                user,
                                address,
                                temp_dest_file,
                                dest_file
                            )
                            # Force oVirt Engine to refresh the list of files
                            # in the ISO domain
                            self.refresh_iso_domain(id)
                            logging.info(
                                _("%s uploaded successfully"), filename
                            )
                        else:
                            logging.error(
                                _(
                                    'There is not enough space in %s '
                                    '(%s bytes) for %s (%s bytes)'
                                ),
                                path,
                                dir_size,
                                filename,
                                file_size
                            )
                    else:
                        ExitCodes.exit_code = ExitCodes.UPLOAD_ERR
                        logging.error(
                            _(
                                '%s exists on %s.  Either remove it or supply '
                                'the --force option to overwrite it.'
                            ),
                            filename,
                            address
                        )
                except Exception, e:
                    ExitCodes.exit_code = ExitCodes.UPLOAD_ERR
                    logging.error(
                        _(
                            'Unable to copy %s to ISO storage '
                            'domain on %s.'
                        ),
                        filename,
                        self.configuration.get('iso_domain')
                    )
                    logging.error(
                        _('Error message is "%s"'),
                        str(e).strip()
                    )
        elif domain_type in ('localfs', ):
            ExitCodes.exit_code = ExitCodes.UPLOAD_ERR
            logging.error(
                _(
                    'Upload to a local storage domain is supported only '
                    'through SSH'
                ),
            )
        else:
            # NFS support.
            tmpDir = tempfile.mkdtemp()
            logging.debug('local NFS mount point is %s' % tmpDir)
            cmd = self.format_nfs_command(address, path, tmpDir)
            try:
                self.caller.call(cmd)
                getpwnam(NFS_USER)
                for filename in self.configuration.files:
                    logging.info(_("Start uploading %s "), filename)
                    dest_dir = os.path.join(
                        tmpDir,
                        remote_path
                    )
                    dest_file = os.path.join(
                        dest_dir,
                        os.path.basename(filename)
                    )
                    retVal = self.exists_nfs(
                        dest_file,
                        NUMERIC_VDSM_ID,
                        NUMERIC_VDSM_ID
                    )
                    if conf.get('force') or not retVal:
                        try:
                            # Remove the file if it exists before
                            # checking space.
                            if retVal:
                                self.remove_file_nfs(
                                    dest_file,
                                    NUMERIC_VDSM_ID,
                                    NUMERIC_VDSM_ID
                                )
                            (dir_size, file_size) = self.space_test_nfs(
                                dest_dir,
                                filename,
                                NUMERIC_VDSM_ID,
                                NUMERIC_VDSM_ID
                            )
                            if (dir_size > file_size):
                                temp_dest_file = os.path.join(
                                    dest_dir,
                                    '.%s' % os.path.basename(filename)
                                )
                                if self.copy_file(
                                    filename,
                                    temp_dest_file,
                                    NUMERIC_VDSM_ID,
                                    NUMERIC_VDSM_ID
                                ):
                                    if self.rename_file_nfs(
                                        temp_dest_file,
                                        dest_file,
                                        NUMERIC_VDSM_ID,
                                        NUMERIC_VDSM_ID
                                    ):
                                        if id is not None:
                                            # Force oVirt Engine to refresh
                                            # the list
                                            # of files in the ISO domain
                                            self.refresh_iso_domain(id)
                                        logging.info(
                                            _(
                                                '{f} uploaded successfully'
                                            ).format(
                                                f=filename,
                                            )
                                        )
                            else:
                                logging.error(
                                    _(
                                        'There is not enough space in %s '
                                        '(%s bytes) for %s (%s bytes)'
                                    ),
                                    path,
                                    dir_size,
                                    filename,
                                    file_size
                                )
                        except Exception, e:
                            ExitCodes.exit_code = ExitCodes.UPLOAD_ERR
                            logging.error(
                                _(
                                    'Unable to copy %s to ISO storage '
                                    'domain on %s.'
                                ),
                                filename,
                                (
                                    self.configuration.get('iso_domain')
                                    if (
                                        self.configuration.get('iso_domain')
                                        is not None
                                    )
                                    else self.configuration.get('nfs_server')
                                )
                            )
                            logging.error(
                                _('Error message is "%s"'),
                                str(e).strip()
                            )
                    else:
                        ExitCodes.exit_code = ExitCodes.UPLOAD_ERR
                        logging.error(
                            _(
                                '%s exists on %s.  Either remove it or '
                                'supply the --force option to overwrite it.'
                            ),
                            filename,
                            address
                        )

            except KeyError:
                ExitCodes.exit_code = ExitCodes.CRITICAL
                logging.error(
                    _(
                        "A user named %s with a UID and GID of %d must be "
                        "defined on the system to mount the ISO storage "
                        "domain on %s as Read/Write"
                    ),
                    NFS_USER,
                    NUMERIC_VDSM_ID,
                    self.configuration.get('iso_domain')
                )
            except Exception, e:
                ExitCodes.exit_code = ExitCodes.CRITICAL
                logging.error(e)
            finally:
                try:
                    cmd = '%s %s %s' % (UMOUNT, NFS_UMOUNT_OPTS, tmpDir)
                    logging.debug(cmd)
                    self.caller.call(cmd)
                    shutil.rmtree(tmpDir)
                except Exception, e:
                    ExitCodes.exit_code = ExitCodes.CLEANUP_ERR
                    logging.debug(e)

if __name__ == '__main__':

    # i18n setup
    gettext.bindtextdomain(APP_NAME)
    gettext.textdomain(APP_NAME)
    _ = gettext.gettext

    usage_string = "\n".join(
        (
            "%prog [options] list ",
            "       %prog [options] upload FILE [FILE]...[FILE]"
        )
    )

    desc = _(
        """The ISO uploader can be used to list ISO storage domains
and upload files to storage domains.  The upload operation supports
multiple files (separated by spaces) and wildcarding."""
    )

    epilog_string = """\nReturn values:
    0: The program ran to completion with no errors.
    1: The program encountered a critical failure and stopped.
    2: The program did not discover any ISO domains.
    3: The program encountered a problem uploading to an ISO domain.
    4: The program encountered a problem un-mounting and removing the
       temporary directory.
"""
    OptionParser.format_epilog = lambda self, formatter: self.epilog

    parser = OptionParser(
        usage_string,
        version=_("Version ") + VERSION,
        description=desc,
        epilog=epilog_string
    )

    parser.add_option(
        "", "--quiet", dest="quiet", action="store_true",
        help=(
            "intended to be used with \"upload\" operations to reduce "
            "console output. (default=False)"
        ),
        default=False)

    parser.add_option(
        "", "--log-file", dest="log_file",
        help=_("path to log file (default=%s)" % DEFAULT_LOG_FILE),
        metavar=_("PATH"),
        default=DEFAULT_LOG_FILE
    )

    parser.add_option(
        "", "--conf-file",
        dest="conf_file",
        help=_(
            "path to configuration file (default=%s)" % (
                DEFAULT_CONFIGURATION_FILE
            )
        ),
        metavar=_("PATH")
    )

    parser.add_option(
        "", "--cert-file", dest="cert_file",
        help=(
            "The CA certificate used to validate the engine. "
            "(default=/etc/pki/ovirt-engine/ca.pem)"
        ),
        metavar="/etc/pki/ovirt-engine/ca.pem",
        default="/etc/pki/ovirt-engine/ca.pem"
    )

    parser.add_option(
        "", "--insecure", dest="insecure",
        help="Do not make an attempt to verify the engine.",
        action="store_true",
        default=False
    )

    parser.add_option(
        "-v", "--verbose", dest="verbose",
        action="store_true", default=False
    )

    parser.add_option(
        "-f",
        "--force",
        dest="force",
        help=_(
            "replace like named files on the target file server (default=off)"
        ),
        action="store_true",
        default=False
    )

    engine_group = OptionGroup(
        parser,
        _("oVirt Engine Configuration"),
        _(
            'The options in the oVirt Engine group are used by the tool '
            'to gain authorization to the oVirt Engine REST API. The '
            'options in this group are available for both list and '
            'upload commands.'
        )
    )

    engine_group.add_option(
        "-u", "--user", dest="user",
        help=_(
            "username to use with the oVirt Engine REST API. "
            "This should be in UPN format."
        ),
        metavar=_("user@engine.example.com")
    )

    engine_group.add_option(
        "-p", "--passwd", dest="passwd",
        help=SUPPRESS_HELP
    )

    engine_group.add_option(
        "",
        "--with-kerberos",
        dest="kerberos",
        help=_(
            "Enable Kerberos authentication instead of the default "
            "basic authentication."
        ),
        action="store_true",
        default=False
    )

    engine_group.add_option(
        "-r", "--engine", dest="engine", metavar="engine.example.com",
        help=_(
            'hostname or IP address of the oVirt Engine '
            '(default=localhost:443).'
        ),
        default="localhost:443"
    )

    iso_group = OptionGroup(
        parser,
        _("ISO Storage Domain Configuration"),
        _(
            'The options in the upload configuration group should be '
            'provided to specify the ISO storage domain to '
            'which files should be uploaded.'
        )
    )

    iso_group.add_option(
        "-i", "--iso-domain", dest="iso_domain",
        help=_("the ISO domain to which the file(s) should be uploaded"),
        metavar=_("ISODOMAIN")
    )

    iso_group.add_option(
        "-n", "--nfs-server", dest="nfs_server",
        help=_(
            'the NFS server to which the file(s) should be uploaded. '
            'This option is an alternative to iso-domain and should not '
            ' be combined with iso-domain.  Use this when you want to '
            'upload files to a specific NFS server '
            '(e.g.--nfs-server=example.com:/path/to/some/dir)'
        ),
        metavar=_("NFSSERVER")
    )

    ssh_group = OptionGroup(
        parser,
        _("Connection Configuration"),
        _(
            'By default the program uses NFS to copy files to the ISO '
            'storage domain. To use SSH file transfer, instead of NFS, '
            'provide a ssh-user.'
        )
    )

    ssh_group.add_option(
        "", "--ssh-user", dest="ssh_user",
        help=_(
            'the SSH user that the program will use for SSH file transfers. '
            'This user must either be root or a user with a UID and GID of '
            '36 on the target file server.'
        ),
        metavar="root"
    )

    ssh_group.add_option(
        "", "--ssh-port", dest="ssh_port",
        help=_("the SSH port to connect on"), metavar="PORT",
        default=22
    )

    ssh_group.add_option(
        "-k", "--key-file", dest="key_file",
        help=_(
            'the identity file (private key) to be used for accessing '
            ' the file server. If a identity file is not supplied the '
            ' program will prompt for a password.  It is strongly '
            ' recommended to use key based authentication '
            ' with SSH because the program may make multiple SSH connections '
            ' resulting in multiple requests for the SSH password.'
        ),
        metavar="KEYFILE"
    )

    parser.add_option_group(engine_group)
    parser.add_option_group(iso_group)
    parser.add_option_group(ssh_group)

    try:
        # Define configuration so that we don't get a NameError
        # when there is an exception in Configuration
        conf = None
        conf = Configuration(parser)

        isoup = ISOUploader(conf)
    except KeyboardInterrupt, k:
        print _("Exiting on user cancel.")
    except NEISODomain, e:
        logging.error("%s" % e)
        sys.exit(ExitCodes.UPLOAD_ERR)
    except Exception, e:
        # FIXME: add better exceptions handling
        logging.error("%s" % e)
        logging.info(_("Use the -h option to see usage."))
        if conf and (conf.get("verbose")):
            logging.debug(_("Configuration:"))
            logging.debug(_("command: %s") % conf.command)
            multilog(logging.debug, traceback.format_exc())
        sys.exit(ExitCodes.CRITICAL)

    sys.exit(ExitCodes.exit_code)
