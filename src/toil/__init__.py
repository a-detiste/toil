# Copyright (C) 2015-2021 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import errno
import logging
import os
import re
import socket
import sys
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Tuple

import requests
from pytz import timezone

from docker.errors import ImageNotFound
from toil.lib.memoize import memoize
from toil.lib.retry import retry
from toil.version import currentCommit

if TYPE_CHECKING:
    from toil.common import Config

log = logging.getLogger(__name__)


def which(cmd, mode=os.F_OK | os.X_OK, path=None) -> Optional[str]:
    """
    Return the path with conforms to the given mode on the Path.

    [Copy-pasted in from python3.6's shutil.which().]

    `mode` defaults to os.F_OK | os.X_OK. `path` defaults to the result
    of os.environ.get("PATH"), or can be overridden with a custom search
    path.
    
    :returns: The path found, or None.
    """
    # Check that a given file can be accessed with the correct mode.
    # Additionally check that `file` is not a directory, as on Windows
    # directories pass the os.access check.
    def _access_check(fn, mode):
        return (os.path.exists(fn) and os.access(fn, mode)
                and not os.path.isdir(fn))

    # If we're given a path with a directory part, look it up directly rather
    # than referring to PATH directories. This includes checking relative to the
    # current directory, e.g. ./script
    if os.path.dirname(cmd):
        if _access_check(cmd, mode):
            return cmd
        return None

    if path is None:
        path = os.environ.get("PATH", os.defpath)
    if not path:
        return None
    path = path.split(os.pathsep)

    if sys.platform == "win32":
        # The current directory takes precedence on Windows.
        if not os.curdir in path:
            path.insert(0, os.curdir)

        # PATHEXT is necessary to check on Windows.
        pathext = os.environ.get("PATHEXT", "").split(os.pathsep)
        # See if the given file matches any of the expected path extensions.
        # This will allow us to short circuit when given "python.exe".
        # If it does match, only test that one, otherwise we have to try
        # others.
        if any(cmd.lower().endswith(ext.lower()) for ext in pathext):
            files = [cmd]
        else:
            files = [cmd + ext for ext in pathext]
    else:
        # On other platforms you don't have things like PATHEXT to tell you
        # what file suffixes are executable, so just pass on cmd as-is.
        files = [cmd]

    seen = set()
    for dir in path:
        normdir = os.path.normcase(dir)
        if not normdir in seen:
            seen.add(normdir)
            for thefile in files:
                name = os.path.join(dir, thefile)
                if _access_check(name, mode):
                    return name
    return None


def toilPackageDirPath() -> str:
    """
    Return the absolute path of the directory that corresponds to the top-level toil package.

    The return value is guaranteed to end in '/toil'.
    """
    result = os.path.dirname(os.path.realpath(__file__))
    if not result.endswith('/toil'):
        raise RuntimeError("The top-level toil package is not named Toil.")
    return result


def inVirtualEnv() -> bool:
    """Test if we are inside a virtualenv or Conda virtual environment."""
    return ('VIRTUAL_ENV' in os.environ or
            'CONDA_DEFAULT_ENV' in os.environ or
            hasattr(sys, 'real_prefix') or
            (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix))


def resolveEntryPoint(entryPoint: str) -> str:
    """
    Find the path to the given entry point that *should* work on a worker.

    :returns: The path found, which may be an absolute or a relative path.
    """
    if os.environ.get("TOIL_CHECK_ENV", None) == 'True' and inVirtualEnv():
        path = os.path.join(os.path.dirname(sys.executable), entryPoint)
        # Inside a virtualenv we try to use absolute paths to the entrypoints.
        if os.path.isfile(path):
            # If the entrypoint is present, Toil must have been installed into the virtualenv (as
            # opposed to being included via --system-site-packages). For clusters this means that
            # if Toil is installed in a virtualenv on the leader, it must be installed in
            # a virtualenv located at the same path on each worker as well.
            if not os.access(path, os.X_OK):
                raise RuntimeError("Cannot access the Toil virtualenv. If installed in a virtualenv on a cluster, make sure that the virtualenv path is the same for the leader and workers.")
            return path
    # Otherwise, we aren't in a virtualenv, or we're in a virtualenv but Toil
    # came in via --system-site-packages, or we think the virtualenv might not
    # exist on the workers.
    return entryPoint


@memoize
def physicalMemory() -> int:
    """
    Calculate the total amount of physical memory, in bytes.

    >>> n = physicalMemory()
    >>> n > 0
    True
    >>> n == physicalMemory()
    True
    """
    try:
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except ValueError:
        import subprocess
        return int(subprocess.check_output(['sysctl', '-n', 'hw.memsize']).decode('utf-8').strip())


def physicalDisk(directory: str) -> int:
    diskStats = os.statvfs(directory)
    return diskStats.f_frsize * diskStats.f_bavail


def applianceSelf(forceDockerAppliance: bool = False) -> str:
    """
    Return the fully qualified name of the Docker image to start Toil appliance containers from.

    The result is determined by the current version of Toil and three environment variables:
    ``TOIL_DOCKER_REGISTRY``, ``TOIL_DOCKER_NAME`` and ``TOIL_APPLIANCE_SELF``.

    ``TOIL_DOCKER_REGISTRY`` specifies an account on a publicly hosted docker registry like Quay
    or Docker Hub. The default is UCSC's CGL account on Quay.io where the Toil team publishes the
    official appliance images. ``TOIL_DOCKER_NAME`` specifies the base name of the image. The
    default of `toil` will be adequate in most cases. ``TOIL_APPLIANCE_SELF`` fully qualifies the
    appliance image, complete with registry, image name and version tag, overriding both
    ``TOIL_DOCKER_NAME`` and `TOIL_DOCKER_REGISTRY`` as well as the version tag of the image.
    Setting TOIL_APPLIANCE_SELF will not be necessary in most cases.
    """
    import toil.version
    registry = lookupEnvVar(name='docker registry',
                            envName='TOIL_DOCKER_REGISTRY',
                            defaultValue=toil.version.dockerRegistry)
    name = lookupEnvVar(name='docker name',
                        envName='TOIL_DOCKER_NAME',
                        defaultValue=toil.version.dockerName)
    appliance = lookupEnvVar(name='docker appliance',
                             envName='TOIL_APPLIANCE_SELF',
                             defaultValue=registry + '/' + name + ':' + toil.version.dockerTag)

    checkDockerSchema(appliance)

    if forceDockerAppliance:
        return appliance
    else:
        return checkDockerImageExists(appliance=appliance)


def customDockerInitCmd() -> str:
    """
    Return the custom command set by the ``TOIL_CUSTOM_DOCKER_INIT_COMMAND`` environment variable.

    The custom docker command is run prior to running the workers and/or the primary node's services.

    This can be useful for doing any custom initialization on instances (e.g. authenticating to
    private docker registries). Any single quotes are escaped and the command cannot contain a
    set of blacklisted chars (newline or tab).

    :returns: The custom commmand, or an empty string is returned if the environment variable is not set.
    """
    command = lookupEnvVar(name='user-defined custom docker init command',
                           envName='TOIL_CUSTOM_DOCKER_INIT_COMMAND',
                           defaultValue='')
    _check_custom_bash_cmd(command)
    return command.replace("'", "'\\''")  # Ensure any single quotes are escaped.


def customInitCmd() -> str:
    """
    Return the custom command set by the ``TOIL_CUSTOM_INIT_COMMAND`` environment variable.

    The custom init command is run prior to running Toil appliance itself in workers and/or the
    primary node (i.e. this is run one stage before ``TOIL_CUSTOM_DOCKER_INIT_COMMAND``).
    
    This can be useful for doing any custom initialization on instances (e.g. authenticating to
    private docker registries). Any single quotes are escaped and the command cannot contain a
    set of blacklisted chars (newline or tab).

    returns: the custom command or n empty string is returned if the environment variable is not set.
    """
    command = lookupEnvVar(name='user-defined custom init command',
                           envName='TOIL_CUSTOM_INIT_COMMAND',
                           defaultValue='')
    _check_custom_bash_cmd(command)
    return command.replace("'", "'\\''")  # Ensure any single quotes are escaped.


def _check_custom_bash_cmd(cmd_str):
    """Ensure that the Bash command doesn't contain invalid characters."""
    if re.search(r'[\n\r\t]', cmd_str):
        raise RuntimeError(f'"{cmd_str}" contains invalid characters (newline and/or tab).')


def lookupEnvVar(name: str, envName: str, defaultValue: str) -> str:
    """
    Look up environment variables that control Toil and log the result.

    :param name: the human readable name of the variable
    :param envName: the name of the environment variable to lookup
    :param defaultValue: the fall-back value
    :return: the value of the environment variable or the default value the variable is not set
    """
    try:
        value = os.environ[envName]
    except KeyError:
        log.info('Using default %s of %s as %s is not set.', name, defaultValue, envName)
        return defaultValue
    else:
        log.info('Overriding %s of %s with %s from %s.', name, defaultValue, value, envName)
        return value


def checkDockerImageExists(appliance: str) -> str:
    """
    Attempt to check a url registryName for the existence of a docker image with a given tag.

    :param appliance: The url of a docker image's registry (with a tag) of the form:
                      'quay.io/<repo_path>:<tag>' or '<repo_path>:<tag>'.
                      Examples: 'quay.io/ucsc_cgl/toil:latest', 'ubuntu:latest', or
                      'broadinstitute/genomes-in-the-cloud:2.0.0'.
    :return: Raises an exception if the docker image cannot be found or is invalid.  Otherwise, it
             will return the appliance string.
    """
    if currentCommit in appliance:
        return appliance
    registryName, imageName, tag = parseDockerAppliance(appliance)

    if registryName == 'docker.io':
        return requestCheckDockerIo(origAppliance=appliance, imageName=imageName, tag=tag)
    else:
        return requestCheckRegularDocker(origAppliance=appliance, registryName=registryName, imageName=imageName,
                                         tag=tag)


def parseDockerAppliance(appliance: str) -> Tuple[str, str, str]:
    """
    Derive parsed registry, image reference, and tag from a docker image string.

    Example: "quay.io/ucsc_cgl/toil:latest"
    Should return: "quay.io", "ucsc_cgl/toil", "latest"

    If a registry is not defined, the default is: "docker.io"
    If a tag is not defined, the default is: "latest"

    :param appliance: The full url of the docker image originally
                      specified by the user (or the default).
                      e.g. "quay.io/ucsc_cgl/toil:latest"
    :returns: registryName, imageName, tag
    """
    appliance = appliance.lower()

    # get the tag
    if ':' in appliance:
        tag = appliance.split(':')[-1]
        appliance = appliance[:-(len(':' + tag))]  # remove only the tag
    else:
        # default to 'latest' if no tag is specified
        tag = 'latest'

    # get the registry and image
    registryName = 'docker.io'  # default if not specified
    imageName = appliance  # will be true if not specified
    if '/' in appliance and '.' in appliance.split('/')[0]:
        registryName = appliance.split('/')[0]
        imageName = appliance[len(registryName):]
    registryName = registryName.strip('/')
    imageName = imageName.strip('/')

    return registryName, imageName, tag


def checkDockerSchema(appliance):
    if not appliance:
        raise ImageNotFound("No docker image specified.")
    elif '://' in appliance:
        raise ImageNotFound("Docker images cannot contain a schema (such as '://'): %s"
                            "" % appliance)
    elif len(appliance) > 256:
        raise ImageNotFound("Docker image must be less than 256 chars: %s"
                            "" % appliance)


class ApplianceImageNotFound(ImageNotFound):
    """
    Error raised when using TOIL_APPLIANCE_SELF results in an HTTP error.

    :param str origAppliance: The full url of the docker image originally
                              specified by the user (or the default).
                              e.g. "quay.io/ucsc_cgl/toil:latest"
    :param str url: The URL at which the image's manifest is supposed to appear
    :param int statusCode: the failing HTTP status code returned by the URL
    """

    def __init__(self, origAppliance, url, statusCode):
        msg = ("The docker image that TOIL_APPLIANCE_SELF specifies (%s) produced "
               "a nonfunctional manifest URL (%s). The HTTP status returned was %s. "
               "The specifier is most likely unsupported or malformed.  "
               "Please supply a docker image with the format: "
               "'<websitehost>.io/<repo_path>:<tag>' or '<repo_path>:<tag>' "
               "(for official docker.io images).  Examples: "
               "'quay.io/ucsc_cgl/toil:latest', 'ubuntu:latest', or "
               "'broadinstitute/genomes-in-the-cloud:2.0.0'."
               "" % (origAppliance, url, str(statusCode)))
        super().__init__(msg)

# Cache images we know exist so we don't have to ask the registry about them
# all the time.
KNOWN_EXTANT_IMAGES = set()

def requestCheckRegularDocker(origAppliance: str, registryName: str, imageName: str, tag: str) -> bool:
    """
    Check if an image exists using the requests library.

    URL is based on the
    `docker v2 schema <https://docs.docker.com/registry/spec/manifest-v2-2/>`_.

    This has the following format: ``https://{websitehostname}.io/v2/{repo}/manifests/{tag}``

    Does not work with the official (docker.io) site, because they require an OAuth token, so a
    separate check is done for docker.io images.

    :param origAppliance: The full url of the docker image originally
                          specified by the user (or the default).
                           e.g. ``quay.io/ucsc_cgl/toil:latest``
    :param registryName: The url of a docker image's registry.  e.g. ``quay.io``
    :param imageName: The image, including path and excluding the tag. e.g. ``ucsc_cgl/toil``
    :param tag: The tag used at that docker image's registry.  e.g. ``latest``
    :raises: ApplianceImageNotFound if no match is found.
    :return: Return True if match found.
    """
    if origAppliance in KNOWN_EXTANT_IMAGES:
        # Check the cache first
        return origAppliance

    ioURL = 'https://{webhost}/v2/{pathName}/manifests/{tag}' \
            ''.format(webhost=registryName, pathName=imageName, tag=tag)
    response = requests.head(ioURL)
    if not response.ok:
        raise ApplianceImageNotFound(origAppliance, ioURL, response.status_code)
    else:
        KNOWN_EXTANT_IMAGES.add(origAppliance)
        return origAppliance


def requestCheckDockerIo(origAppliance: str, imageName: str, tag: str) -> bool:
    """
    Check docker.io to see if an image exists using the requests library.

    URL is based on the docker v2 schema.  Requires that an access token be fetched first.

    :param origAppliance: The full url of the docker image originally
                          specified by the user (or the default).  e.g. "ubuntu:latest"
    :param imageName: The image, including path and excluding the tag. e.g. "ubuntu"
    :param tag: The tag used at that docker image's registry.  e.g. "latest"
    :raises: ApplianceImageNotFound if no match is found.
    :return: Return True if match found.
    """
    if origAppliance in KNOWN_EXTANT_IMAGES:
        # Check the cache first
        return origAppliance

    # only official images like 'busybox' or 'ubuntu'
    if '/' not in imageName:
        imageName = 'library/' + imageName

    token_url = 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull'.format(
        repo=imageName)
    requests_url = f'https://registry-1.docker.io/v2/{imageName}/manifests/{tag}'

    token = requests.get(token_url)
    jsonToken = token.json()
    bearer = jsonToken["token"]
    response = requests.head(requests_url, headers={'Authorization': f'Bearer {bearer}'})
    if not response.ok:
        raise ApplianceImageNotFound(origAppliance, requests_url, response.status_code)
    else:
        KNOWN_EXTANT_IMAGES.add(origAppliance)
        return origAppliance


def logProcessContext(config: "Config") -> None:
    # toil.version.version (string) cannot be imported at top level because it conflicts with
    # toil.version (module) and Sphinx doesn't like that.
    from toil.version import version
    log.info("Running Toil version %s on host %s.", version, socket.gethostname())
    log.debug("Configuration: %s", config.__dict__)


try:
    from boto import provider
    from botocore.credentials import (JSONFileCache,
                                      RefreshableCredentials,
                                      create_credential_resolver)
    from botocore.session import Session

    cache_path = '~/.cache/aws/cached_temporary_credentials'
    datetime_format = "%Y-%m-%dT%H:%M:%SZ"  # incidentally the same as the format used by AWS
    log = logging.getLogger(__name__)


    # But in addition to our manual cache, we also are going to turn on boto3's
    # new built-in caching layer.

    def datetime_to_str(dt):
        """
        Convert a naive (implicitly UTC) datetime object into a string, explicitly UTC.

        >>> datetime_to_str(datetime(1970, 1, 1, 0, 0, 0))
        '1970-01-01T00:00:00Z'
        """
        return dt.strftime(datetime_format)


    def str_to_datetime(s):
        """
        Convert a string, explicitly UTC into a naive (implicitly UTC) datetime object.

        >>> str_to_datetime( '1970-01-01T00:00:00Z' )
        datetime.datetime(1970, 1, 1, 0, 0)

        Just to show that the constructor args for seconds and microseconds are optional:
        >>> datetime(1970, 1, 1, 0, 0, 0)
        datetime.datetime(1970, 1, 1, 0, 0)
        """
        return datetime.strptime(s, datetime_format)


    class BotoCredentialAdapter(provider.Provider):
        """
        Boto 2 Adapter to use AWS credentials obtained via Boto 3's credential finding logic.

        This allows for automatic role assumption
        respecting the Boto 3 config files, even when parts of the app still use
        Boto 2.

        This class also handles caching credentials in multi-process environments
        to avoid loads of processes swamping the EC2 metadata service.
        """

        # TODO: We take kwargs because new boto2 versions have an 'anon'
        # argument and we want to be future proof

        def __init__(self, name, access_key=None, secret_key=None,
                     security_token=None, profile_name=None, **kwargs):
            """Create a new BotoCredentialAdapter."""
            # TODO: We take kwargs because new boto2 versions have an 'anon'
            # argument and we want to be future proof

            if (name == 'aws' or name is None) and access_key is None and not kwargs.get('anon', False):
                # We are on AWS and we don't have credentials passed along and we aren't anonymous.
                # We will backend into a boto3 resolver for getting credentials.
                # Make sure to enable boto3's own caching, so we can share that
                # cache with pure boto3 code elsewhere in Toil.
                # Keep synced with toil.lib.aws.session.establish_boto3_session
                self._boto3_resolver = create_credential_resolver(Session(profile=profile_name), cache=JSONFileCache())
            else:
                # We will use the normal flow
                self._boto3_resolver = None

            # Pass along all the arguments
            super().__init__(name, access_key=access_key,
                                                        secret_key=secret_key, security_token=security_token,
                                                        profile_name=profile_name, **kwargs)

        def get_credentials(self, access_key=None, secret_key=None, security_token=None, profile_name=None):
            """
            Make sure our credential fields are populated.

            Called by the base class constructor.
            """
            if self._boto3_resolver is not None:
                # Go get the credentials from the cache, or from boto3 if not cached.
                # We need to be eager here; having the default None
                # _credential_expiry_time makes the accessors never try to refresh.
                self._obtain_credentials_from_cache_or_boto3()
            else:
                # We're not on AWS, or they passed a key, or we're anonymous.
                # Use the normal route; our credentials shouldn't expire.
                super().get_credentials(access_key=access_key,
                                                                   secret_key=secret_key, security_token=security_token,
                                                                   profile_name=profile_name)

        def _populate_keys_from_metadata_server(self):
            """
            Hack to catch _credential_expiry_time being too soon and refresh the credentials.

            This override is misnamed; it's actually the only hook we have to catch
            _credential_expiry_time being too soon and refresh the credentials. We
            actually just go back and poke the cache to see if it feels like
            getting us new credentials.

            Boto 2 hardcodes a refresh within 5 minutes of expiry:
            https://github.com/boto/boto/blob/591911db1029f2fbb8ba1842bfcc514159b37b32/boto/provider.py#L247

            Boto 3 wants to refresh 15 or 10 minutes before expiry:
            https://github.com/boto/botocore/blob/8d3ea0e61473fba43774eb3c74e1b22995ee7370/botocore/credentials.py#L279

            So if we ever want to refresh, Boto 3 wants to refresh too.
            """
            # This should only happen if we have expiring credentials, which we should only get from boto3
            if self._boto3_resolver is None:
                raise RuntimeError("The Boto3 resolver should not be None.")

            self._obtain_credentials_from_cache_or_boto3()

        @retry()
        def _obtain_credentials_from_boto3(self):
            """
            Fill our credential fields from Boto 3.

            We know the current cached credentials are not good, and that we
            need to get them from Boto 3. Fill in our credential fields
            (_access_key, _secret_key, _security_token,
            _credential_expiry_time) from Boto 3.
            """
            # We get a Credentials object
            # <https://github.com/boto/botocore/blob/8d3ea0e61473fba43774eb3c74e1b22995ee7370/botocore/credentials.py#L227>
            # or a RefreshableCredentials, or None on failure.
            creds = self._boto3_resolver.load_credentials()

            if creds is None:
                try:
                    resolvers = str(self._boto3_resolver.providers)
                except:
                    resolvers = "(Resolvers unavailable)"
                raise RuntimeError("Could not obtain AWS credentials from Boto3. Resolvers tried: " + resolvers)

            # Make sure the credentials actually has some credentials if it is lazy
            creds.get_frozen_credentials()

            # Get when the credentials will expire, if ever
            if isinstance(creds, RefreshableCredentials):
                # Credentials may expire.
                # Get a naive UTC datetime like boto 2 uses from the boto 3 time.
                self._credential_expiry_time = creds._expiry_time.astimezone(timezone('UTC')).replace(tzinfo=None)
            else:
                # Credentials never expire
                self._credential_expiry_time = None

            # Then, atomically get all the credentials bits. They may be newer than we think they are, but never older.
            frozen = creds.get_frozen_credentials()

            # Copy them into us
            self._access_key = frozen.access_key
            self._secret_key = frozen.secret_key
            self._security_token = frozen.token

        def _obtain_credentials_from_cache_or_boto3(self):
            """
            Get the cached credentials.

            Or retrieve them from Boto 3 and cache them
            (or wait for another cooperating process to do so) if they are missing
            or not fresh enough.
            """
            cache_path = '~/.cache/aws/cached_temporary_credentials'
            path = os.path.expanduser(cache_path)
            tmp_path = path + '.tmp'
            while True:
                log.debug('Attempting to read cached credentials from %s.', path)
                try:
                    with open(path) as f:
                        content = f.read()
                        if content:
                            record = content.split('\n')
                            if len(record) != 4:
                                raise RuntimeError("Number of cached credentials is not 4.")
                            self._access_key = record[0]
                            self._secret_key = record[1]
                            self._security_token = record[2]
                            self._credential_expiry_time = str_to_datetime(record[3])
                        else:
                            log.debug('%s is empty. Credentials are not temporary.', path)
                            self._obtain_credentials_from_boto3()
                            return
                except OSError as e:
                    if e.errno == errno.ENOENT:
                        log.debug('Cached credentials are missing.')
                        dir_path = os.path.dirname(path)
                        if not os.path.exists(dir_path):
                            log.debug('Creating parent directory %s', dir_path)
                            try:
                                # A race would be ok at this point
                                os.makedirs(dir_path, exist_ok=True)
                            except OSError as e2:
                                if e2.errno == errno.EROFS:
                                    # Sometimes we don't actually have write access to ~.
                                    # We may be running in a non-writable Toil container.
                                    # We should just go get our own credentials
                                    log.debug('Cannot use the credentials cache because we are working on a read-only filesystem.')
                                    self._obtain_credentials_from_boto3()
                                else:
                                    raise
                    else:
                        raise
                else:
                    if self._credentials_need_refresh():
                        log.debug('Cached credentials are expired.')
                    else:
                        log.debug('Cached credentials exist and are still fresh.')
                        return
                # We get here if credentials are missing or expired
                log.debug('Racing to create %s.', tmp_path)
                # Only one process, the winner, will succeed
                try:
                    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except OSError as e:
                    if e.errno == errno.EEXIST:
                        log.debug('Lost the race to create %s. Waiting on winner to remove it.', tmp_path)
                        while os.path.exists(tmp_path):
                            time.sleep(0.1)
                        log.debug('Winner removed %s. Trying from the top.', tmp_path)
                    else:
                        raise
                else:
                    try:
                        log.debug('Won the race to create %s.  Requesting credentials from backend.', tmp_path)
                        self._obtain_credentials_from_boto3()
                    except:
                        os.close(fd)
                        fd = None
                        log.debug('Failed to obtain credentials, removing %s.', tmp_path)
                        # This unblocks the losers.
                        os.unlink(tmp_path)
                        # Bail out. It's too likely to happen repeatedly
                        raise
                    else:
                        if self._credential_expiry_time is None:
                            os.close(fd)
                            fd = None
                            log.debug('Credentials are not temporary.  Leaving %s empty and renaming it to %s.',
                                      tmp_path, path)
                            # No need to actually cache permanent credentials,
                            # because we know we aren't getting them from the
                            # metadata server or by assuming a role. Those both
                            # give temporary credentials.
                        else:
                            log.debug('Writing credentials to %s.', tmp_path)
                            with os.fdopen(fd, 'w') as fh:
                                fd = None
                                fh.write('\n'.join([
                                    self._access_key,
                                    self._secret_key,
                                    self._security_token,
                                    datetime_to_str(self._credential_expiry_time)]))
                            log.debug('Wrote credentials to %s. Renaming to %s.', tmp_path, path)
                        os.rename(tmp_path, path)
                        return
                    finally:
                        if fd is not None:
                            os.close(fd)


    provider.Provider = BotoCredentialAdapter

except ImportError:
    pass
