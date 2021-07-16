"""API for interacting with a remote Alyx instance through REST
The AlyxClient class contains methods for making remote Alyx REST queries and downloading remote
files through Alyx.

Examples:
    alyx = AlyxClient(
        username='test_user', password='TapetesBloc18',
        base_url='https://test.alyx.internationalbrainlab.org')

    # List subjects
    subjects = alyx.rest('subjects', 'list')

    # Create a subject
    record = {
        'nickname': nickname,
        'responsible_user': 'olivier',
        'birth_date': '2019-06-15',
        'death_date': None,
        'lab': 'cortexlab',
    }
    new_subj = alyx.rest('subjects', 'create', data=record)

    # Download a remote file, given a local path
    url = 'zadorlab/Subjects/flowers/2018-07-13/1/channels.probe.npy'
    local_path = self.alyx.download_file(url)
"""
import json
import logging
import math
import os
import re
import functools
import urllib.request
from urllib.error import HTTPError
import urllib.parse
from collections.abc import Mapping
from typing import Optional
from datetime import datetime, timedelta
from pathlib import Path
import warnings
import hashlib
import zipfile
import tempfile
from getpass import getpass

import requests
from tqdm import tqdm

from pprint import pprint
import one.params
from iblutil.io import hashfile
from one.util import ensure_list

_logger = logging.getLogger(__name__)


def _cache_response(method):
    """
    Decorator for the generic request method; caches the result of the query and on subsequent
    calls, returns cache instead of hitting the database
    :param method: function to wrap (i.e. AlyxClient._generic_request)
    :return: handle to wrapped method
    """

    @functools.wraps(method)
    def wrapper_decorator(alyx_client, *args, expires=None, clobber=False, **kwargs):
        """
        REST caching wrapper
        :param alyx_client: an instance of the AlyxClient class
        :param args: positional arguments for applying to wrapped function
        :param expires: an optional timedelta for how long cached response is valid.  If True,
        the cached response will not be used on subsequent calls.  If None, the default expiry
        is applied.
        :param clobber: If True any existing cached response is overwritten
        :param kwargs: keyword arguments for applying to wrapped function
        :return: the REST response JSON either from cached file or directly from remote
        endpoint
        """
        expires = expires or alyx_client.default_expiry
        mode = (alyx_client.cache_mode or '').lower()
        if args[0].__name__ != mode and mode != '*':
            return method(alyx_client, *args, **kwargs)
        # Check cache
        rest_cache = one.params.get_rest_dir(alyx_client.base_url)
        sha1 = hashlib.sha1()
        sha1.update(bytes(args[1], 'utf-8'))
        name = sha1.hexdigest()
        # Reversible but length may exceed 255 chars
        # name = base64.urlsafe_b64encode(args[2].encode('UTF-8')).decode('UTF-8')
        files = list(rest_cache.glob(name))
        cached = None
        if len(files) == 1 and not clobber:
            _logger.debug('loading REST response from cache')
            with open(files[0], 'r') as f:
                cached, when = json.load(f)
            if datetime.fromisoformat(when) > datetime.now():
                return cached
        try:
            response = method(alyx_client, *args, **kwargs)
        except requests.exceptions.ConnectionError as ex:
            if cached and not clobber:
                warnings.warn('Failed to connect, returning cached response', RuntimeWarning)
                return cached
            raise ex  # No cache and can't connect to database; re-raise

        # Save response into cache
        rest_cache.mkdir(exist_ok=True, parents=True)
        _logger.debug('caching REST response')
        expiry_datetime = datetime.now() + (timedelta() if expires is True else expires)
        with open(rest_cache / name, 'w') as f:
            json.dump((response, expiry_datetime.isoformat()), f)
        return response

    return wrapper_decorator


class _PaginatedResponse(Mapping):
    """
    This class allows to emulate a list from a paginated response.
    Provides cache functionality
    PaginatedResponse(alyx, response)
    """

    def __init__(self, alyx, rep):
        self.alyx = alyx
        self.count = rep['count']
        self.limit = len(rep['results'])
        # store URL without pagination query params
        self.query = rep['next']
        # init the cache, list with None with count size
        self._cache = [None] * self.count
        # fill the cache with results of the query
        for i in range(self.limit):
            self._cache[i] = rep['results'][i]

    def __len__(self):
        return self.count

    def __getitem__(self, item):
        if isinstance(item, slice):
            while None in self._cache[item]:
                self.populate(self._cache[item].index(None))
        elif self._cache[item] is None:
            self.populate(item)
        return self._cache[item]

    def populate(self, idx):
        offset = self.limit * math.floor(idx / self.limit)
        query = update_url_params(self.query, {'limit': self.limit, 'offset': offset})
        res = self.alyx._generic_request(requests.get, query)
        for i, r in enumerate(res['results']):
            self._cache[i + offset] = res['results'][i]

    def __iter__(self):
        for i in range(self.count):
            yield self.__getitem__(i)


def update_url_params(url: str, params: dict) -> str:
    """Add/update the query parameters of a URL and make url safe

    Parameters
    ----------
    url : str
        A URL string with which to update the query parameters
    params : dict
        A dict of new parameters.  For multiple values for the same query, use a list (see example)

    Returns
    -------
        A new URL with said parameters updated

    Examples
    -------
        update_url_params('website.com/?q=', {'pg': 5})
        'website.com/?pg=5'

        update_url_params('website.com?q=xxx', {'pg': 5, 'foo': ['bar', 'baz']})
        'website.com?q=xxx&pg=5&foo=bar&foo=baz'
    """
    # Remove percent-encoding
    url = urllib.parse.unquote(url)
    parsed_url = urllib.parse.urlsplit(url)
    # Extract URL query arguments and convert to dict
    parsed_get_args = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=False)
    # Merge URL arguments dict with new params
    parsed_get_args.update(params)
    # Convert back to query string
    encoded_get_args = urllib.parse.urlencode(parsed_get_args, doseq=True)
    # Update parser and convert to full URL str
    return parsed_url._replace(query=encoded_get_args).geturl()


def http_download_file_list(links_to_file_list, **kwargs):
    """
    Downloads a list of files from the flat Iron from a list of links.
    Same options behaviour as http_download_file

    :param links_to_file_list: list of http links to files.
    :type links_to_file_list: list

    :return: (list) a list of the local full path of the downloaded files.
    """
    file_names_list = []
    for link_str in links_to_file_list:
        file_names_list.append(http_download_file(link_str, **kwargs))
    return file_names_list


def http_download_file(full_link_to_file, chunks=None, *, clobber=False, silent=False,
                       username='', password='', cache_dir='', return_md5=False, headers=None):
    """
    :param full_link_to_file: http link to the file.
    :type full_link_to_file: str
    :param chunks: chunks to download
    :type chunks: tuple of ints
    :param clobber: [False] If True, force overwrite the existing file.
    :type clobber: bool
    :param username: [''] authentication for password protected file server.
    :type username: str
    :param password: [''] authentication for password protected file server.
    :type password: str
    :param cache_dir: [''] directory in which files are cached; defaults to user's
     Download directory.
    :type cache_dir: str
    :param return_md5: if true an MD5 hash of the file is additionally returned
    :type return_md5: bool
    :param: headers: [{}] additional headers to add to the request (auth tokens etc..)
    :type headers: dict
    :param: silent: [False] suppress download progress bar
    :type silent: bool

    :return: (str) a list of the local full path of the downloaded files.
    """
    if not full_link_to_file:
        return ''

    # makes sure special characters get encoded ('#' in file names for example)
    surl = urllib.parse.urlsplit(full_link_to_file, allow_fragments=False)
    full_link_to_file = surl._replace(path=urllib.parse.quote(surl.path)).geturl()

    # default cache directory is the home dir
    if not cache_dir:
        cache_dir = str(Path.home().joinpath('Downloads'))

    # This is the local file name
    file_name = str(cache_dir) + os.sep + os.path.basename(full_link_to_file)

    # do not overwrite an existing file unless specified
    if not clobber and os.path.exists(file_name):
        return (file_name, hashfile.md5(file_name)) if return_md5 else file_name

    # This should be the base url you wanted to access.
    baseurl = os.path.split(str(full_link_to_file))[0]

    # Create a password manager
    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    if username and password:
        manager.add_password(None, baseurl, username, password)

    # Create an authentication handler using the password manager
    auth = urllib.request.HTTPBasicAuthHandler(manager)

    # Create an opener that will replace the default urlopen method on further calls
    opener = urllib.request.build_opener(auth)
    urllib.request.install_opener(opener)

    # Support for partial download.
    req = urllib.request.Request(full_link_to_file)
    if chunks is not None:
        first_byte, n_bytes = chunks
        req.add_header('Range', 'bytes=%d-%d' % (first_byte, first_byte + n_bytes - 1))

    # add additional headers
    if headers is not None:
        for k in headers:
            req.add_header(k, headers[k])

    # Open the url and get the length
    try:
        u = urllib.request.urlopen(req)
    except HTTPError as e:
        _logger.error(f'{str(e)} {full_link_to_file}')
        raise e

    file_size = int(u.getheader('Content-length'))
    if not silent:
        print(f'Downloading: {file_name} Bytes: {file_size}')
    file_size_dl = 0
    block_sz = 8192 * 64 * 8

    md5 = hashlib.md5()
    f = open(file_name, 'wb')
    with tqdm(total=file_size, disable=silent) as pbar:
        while True:
            buffer = u.read(block_sz)
            if not buffer:
                break
            file_size_dl += len(buffer)
            f.write(buffer)
            if return_md5:
                md5.update(buffer)
            pbar.update(file_size_dl)
    f.close()

    return (file_name, md5.hexdigest()) if return_md5 else file_name


def file_record_to_url(file_records):
    """
    Translate a Json dictionary to an usable http url for downloading files.

    :param file_records: json containing a 'data_url' field
    :type file_records: dict

    :return: urls: (list) a list of strings representing full data urls
    """
    urls = []
    for fr in file_records:
        if fr['data_url'] is not None:
            urls.append(fr['data_url'])
    return urls


def dataset_record_to_url(dataset_record):
    """
    Extracts a list of files urls from a list of dataset queries.

    :param dataset_record: dataset Json from a rest request.
    :type dataset_record: list

    :return: (list) a list of strings representing files urls corresponding to the datasets records
    """
    urls = []
    if isinstance(dataset_record, dict):
        dataset_record = [dataset_record]
    for ds in dataset_record:
        urls += file_record_to_url(ds['file_records'])
    return urls


class AlyxClient():
    """
    Class that implements simple GET/POST wrappers for the Alyx REST API
    http://alyx.readthedocs.io/en/latest/api.html  # FIXME old link
    """
    _token = None
    _headers = None  # Headers for REST requests only
    user = None
    base_url = None

    def __init__(self, base_url=None, username=None, password=None,
                 cache_dir=None, silent=False, cache_rest='GET', stay_logged_in=True):
        """
        Create a client instance that allows to GET and POST to the Alyx server
        For oneibl, constructor attempts to authenticate with credentials in params.py
        For standalone cases, AlyxClient(username='', password='', base_url='')

        :param username: Alyx database user
        :param password: Alyx database password
        :param base_url: Alyx server address, including port and protocol
        :param cache_rest: which type of http method to apply cache to; if '*', all requests are
        cached.
        """
        self.silent = silent
        self._par = one.params.get(client=base_url, silent=self.silent)
        # TODO Pass these to `params.get` and have it deal with setup defaults
        self.base_url = base_url or self._par.ALYX_URL
        self._par = self._par.set('CACHE_DIR', cache_dir or self._par.CACHE_DIR)
        self.authenticate(username, password, cache_token=stay_logged_in)
        self._rest_schemes = None
        # the mixed accept application may cause errors sometimes, only necessary for the docs
        self._headers['Accept'] = 'application/json'
        # REST cache parameters
        # The default length of time that cache file is valid for,
        # The default expiry is overridden by the `expires` kwarg.  If False, the caching is
        # turned off.
        self.default_expiry = timedelta(days=1)
        self.cache_mode = cache_rest
        self._obj_id = id(self)

    @property
    def rest_schemes(self):
        """Delayed fetch of rest schemes speeds up instantiation"""
        if not self._rest_schemes:
            self._rest_schemes = self.get('/docs', expires=timedelta(weeks=1))
        return self._rest_schemes

    @property
    def cache_dir(self):
        return Path(self._par.CACHE_DIR)

    def is_logged_in(self):
        """Check if user logged into Alyx database

        Returns
        -------
            True if user is authenticated
        """
        return self._token and self.user and self._headers and 'Authorization' in self._headers

    def list_endpoints(self):
        """
        Return a list of available REST endpoints

        Returns
        -------
            List of REST endpoint strings
        """
        EXCLUDE = ('_type', '_meta', '', 'auth-token')
        return sorted(x for x in self.rest_schemes.keys() if x not in EXCLUDE)

    @_cache_response
    def _generic_request(self, reqfunction, rest_query, data=None, files=None):
        if not self._token and (not self._headers or 'Authorization' not in self._headers):
            self.authenticate(username=self.user)
        # makes sure the base url is the one from the instance
        rest_query = rest_query.replace(self.base_url, '')
        if not rest_query.startswith('/'):
            rest_query = '/' + rest_query
        _logger.debug(f"{self.base_url + rest_query}, headers: {self._headers}")
        headers = self._headers.copy()
        if files is None:
            data = json.dumps(data) if isinstance(data, dict) or isinstance(data, list) else data
            headers['Content-Type'] = 'application/json'
        if rest_query.startswith('/docs'):
            # the mixed accept application may cause errors sometimes, only necessary for the docs
            headers['Accept'] = 'application/coreapi+json'
        r = reqfunction(self.base_url + rest_query, stream=True, headers=headers,
                        data=data, files=files)
        if r and r.status_code in (200, 201):
            return json.loads(r.text)
        elif r and r.status_code == 204:
            return
        else:
            if not self.silent:
                _logger.error(self.base_url + rest_query)
                _logger.error(r.text)
            raise (requests.HTTPError(r))

    def authenticate(self, username=None, password=None, cache_token=True, force=False):
        """
        Gets a security token from the Alyx REST API to create requests headers.
        Credentials are loaded via one.params

        Parameters
        ----------
        username : str
            Alyx username.  If None, token not cached and not silent, user is prompted.
        password : str
            Alyx password.  If None, token not cached and not silent, user is prompted.
        cache_token : bool
            If true, the token is cached for subsequent auto-logins
        force : bool
            If true, any cached token is ignored
        """
        # Get username
        if username is None:
            username = getattr(self._par, 'ALYX_LOGIN', self.user)
        if username is None and not self.silent:
            username = input('Enter Alyx username:')

        # Check if token cached
        if not force and getattr(self._par, 'TOKEN', False) and username in self._par.TOKEN:
            self._token = self._par.TOKEN[username]
            self._headers = {
                'Authorization': f'Token {list(self._token.values())[0]}',
                'Accept': 'application/json'}
            self.user = username
            return

        # Get password
        if password is None:
            password = getattr(self._par, 'ALYX_PWD', None)
        if password is None and not self.silent:
            password = getpass('Enter Alyx password:')
        try:
            credentials = {'username': username, 'password': password}
            rep = requests.post(self.base_url + '/auth-token', data=credentials)
        except requests.exceptions.ConnectionError:
            raise ConnectionError(
                f"Can't connect to {self.base_url}.\n" +
                "Check your internet connections and Alyx database firewall"
            )
        # Assign token or raise exception on internal server error
        self._token = rep.json() if rep.ok else rep.raise_for_status()
        if not (list(self._token.keys()) == ['token']):
            _logger.error(rep)
            raise Exception('Alyx authentication error. Check your credentials')
        self._headers = {
            'Authorization': 'Token {}'.format(list(self._token.values())[0]),
            'Accept': 'application/json'}
        if cache_token:
            # Update saved pars
            par = one.params.get(client=self.base_url, silent=True)
            tokens = getattr(par, 'TOKEN', {})
            tokens[username] = self._token
            one.params.save(par.set('TOKEN', tokens), self.base_url)
            # Update current pars
            self._par = self._par.set('TOKEN', tokens)
        self.user = username
        if not self.silent:
            print(f"Connected to {self.base_url} as {self.user}")

    def logout(self):
        """Log out from Alyx
        Deletes the cached authentication token for the currently logged-in user
        """
        if not self.is_logged_in():
            return
        par = one.params.get(client=self.base_url, silent=True)
        username = self.user
        # Remove token from cache
        if getattr(par, 'TOKEN', False) and username in par.TOKEN:
            del par.TOKEN[username]
            one.params.save(par, self.base_url)
        # Remove token from local pars
        if getattr(self._par, 'TOKEN', False) and username in self._par.TOKEN:
            del self._par.TOKEN[username]
        # Remove token from object
        self.user = None
        self._token = None
        if self._headers and 'Authorization' in self._headers:
            del self._headers['Authorization']
        self.clear_rest_cache()
        if not self.silent:
            print(f'{username} logged out from {self.base_url}')

    def delete(self, rest_query):
        """
        Sends a DELETE request to the Alyx server. Will raise an exception on any status_code
        other than 200, 201.

        :param rest_query: examples:
         '/weighings/c617562d-c107-432e-a8ee-682c17f9e698'
         'https://test.alyx.internationalbrainlab.org/weighings/c617562d-c107-432e-a8ee-682c17f9e698'.
        :type rest_query: str

        :return: (dict/list) json interpreted dictionary from response
        """
        return self._generic_request(requests.delete, rest_query)

    def download_file(self, url, **kwargs):
        """
        Downloads a file on the Alyx server from a file record REST field URL
        :param url: full url(s) of the file(s)
        :param kwargs: webclient.http_download_file parameters
        :return: local path(s) of downloaded file(s)
        """
        if isinstance(url, str):
            url = self._validate_file_url(url)
            download_fcn = http_download_file
        else:
            url = (self._validate_file_url(x) for x in url)
            download_fcn = http_download_file_list
        pars = dict(
            silent=kwargs.pop('silent', self.silent),
            cache_dir=kwargs.pop('cache_dir', self._par.CACHE_DIR),
            username=self._par.HTTP_DATA_SERVER_LOGIN,
            password=self._par.HTTP_DATA_SERVER_PWD,
            **kwargs
        )
        return download_fcn(url, **pars)

    def download_cache_tables(self):
        """Downloads the Alyx cache tables to the local data cache directory

        Returns
        -------
            List of parquet table file paths
        """
        # query the database for the latest cache; expires=None overrides cached response
        self.cache_dir.mkdir(exist_ok=True)
        if not self.is_logged_in():
            self.authenticate()
        with tempfile.TemporaryDirectory(dir=self.cache_dir) as tmp:
            file = http_download_file(f'{self.base_url}/cache.zip',
                                      headers=self._headers,
                                      silent=self.silent,
                                      cache_dir=tmp,
                                      clobber=True)
            with zipfile.ZipFile(file, 'r') as zipped:
                files = zipped.namelist()
                zipped.extractall(self.cache_dir)
        return [Path(self.cache_dir, table) for table in files]

    def _validate_file_url(self, url):
        """Asserts that URL matches HTTP_DATA_SERVER parameter.
        Currently only one remote HTTP server is supported for a given AlyxClient instance.  If
        the URL contains only the relative path part, the full URL is returned.

        Parameters
        ----------
        url : str
            The full or partial URL to validate

        Returns
        -------
            The complete URL

        Examples
        --------
            url = self._validate_file_url('https://webserver.net/path/to/file')
            'https://webserver.net/path/to/file'
            url = self._validate_file_url('path/to/file')
            'https://webserver.net/path/to/file'
        """
        if url.startswith('http'):  # A full URL
            assert url.startswith(self._par.HTTP_DATA_SERVER), \
                ('remote protocol and/or hostname does not match HTTP_DATA_SERVER parameter:\n' +
                 f'"{url[:40]}..." should start with "{self._par.HTTP_DATA_SERVER}"')
        elif not url.startswith(self._par.HTTP_DATA_SERVER):
            url = self.rel_path2url(url)
        return url

    def rel_path2url(self, path):
        """Given a relative file path, return the remote HTTP server URL.
        It is expected that the remote HTTP server has the same file tree as the local system.

        Parameters
        ----------
        path : str, pathlib.Path
            A relative ALF path (subject/date/number/etc.)

        Returns
        -------
            A URL string
        """
        path = str(path).strip('/')
        assert not path.startswith('http')
        return f'{self._par.HTTP_DATA_SERVER}/{path}'

    def get(self, rest_query, **kwargs):
        """
        Sends a GET request to the Alyx server. Will raise an exception on any status_code
        other than 200, 201.
        For the dictionary contents and list of endpoints, refer to:
        https://alyx.internationalbrainlab.org/docs

        :param rest_query: example: '/sessions?user=Hamish'.
        :type rest_query: str

        :return: (dict/list) json interpreted dictionary from response
        """
        rep = self._generic_request(requests.get, rest_query, **kwargs)
        _logger.debug(rest_query)
        if isinstance(rep, dict) and list(rep.keys()) == ['count', 'next', 'previous', 'results']:
            if len(rep['results']) < rep['count']:
                rep = _PaginatedResponse(self, rep)
            else:
                rep = rep['results']
        return rep

    def patch(self, rest_query, data=None, files=None):
        """
        Sends a PATCH request to the Alyx server.
        For the dictionary contents, refer to:
        https://alyx.internationalbrainlab.org/docs

        :param rest_query: (required)the endpoint as full or relative URL
        :type rest_query: str
        :param data: json encoded string or dictionary (cf.requests)
        :type data: None, dict or str
        :param files: dictionary / tuple (cf.requests)

        :return: response object
        """
        return self._generic_request(requests.patch, rest_query, data=data, files=files)

    def post(self, rest_query, data=None, files=None):
        """
        Sends a POST request to the Alyx server.
        For the dictionary contents, refer to:
        https://alyx.internationalbrainlab.org/docs

        :param rest_query: (required)the endpoint as full or relative URL
        :type rest_query: str
        :param data: dictionary or json encoded string
        :type data: None, dict or str
        :param files: dictionary / tuple (cf.requests)

        :return: response object
        """
        return self._generic_request(requests.post, rest_query, data=data, files=files)

    def put(self, rest_query, data=None, files=None):
        """
        Sends a PUT request to the Alyx server.
        For the dictionary contents, refer to:
        https://alyx.internationalbrainlab.org/docs

        :param rest_query: (required)the endpoint as full or relative URL
        :type rest_query: str
        :param data: dictionary or json encoded string
        :type data: None, dict or str
        :param files: dictionary / tuple (cf.requests)

        :return: response object
        """
        return self._generic_request(requests.put, rest_query, data=data, files=files)

    def rest(self, url=None, action=None, id=None, data=None, files=None,
             no_cache=False, **kwargs):
        """
        alyx_client.rest(): lists endpoints
        alyx_client.rest(endpoint): lists actions for endpoint
        alyx_client.rest(endpoint, action): lists fields and URL

        Example REST endpoint with all actions:

            client.rest('subjects', 'list')
            client.rest('subjects', 'list', field_filter1='filterval')
            client.rest('subjects', 'create', data=sub_dict)
            client.rest('subjects', 'read', id='nickname')
            client.rest('subjects', 'update', id='nickname', data=sub_dict)
            client.rest('subjects', 'partial_update', id='nickname', data=sub_dict)
            client.rest('subjects', 'delete', id='nickname')
            client.rest('notes', 'create', data=nd, files={'image': open(image_file, 'rb')})

        :param url: endpoint name
        :param action: 'list', 'create', 'read', 'update', 'partial_update', 'delete'
        :param id: lookup string for actions 'read', 'update', 'partial_update', and 'delete'
        :param data: data dictionary for actions 'update', 'partial_update' and 'create'
        :param files: if file upload
        :param no_cache: if true the `list` and `read` actions are performed without caching
        :param ``**kwargs``: filter as per the Alyx REST documentation
            cf. https://alyx.internationalbrainlab.org/docs/
        :return: list of queried dicts ('list') or dict (other actions)
        """
        # if endpoint is None, list available endpoints
        if not url:
            pprint(self.list_endpoints())
            return
        # remove beginning slash if any
        if url.startswith('/'):
            url = url[1:]
        # and split to the next slash or question mark
        endpoint = re.findall("^/*[^?/]*", url)[0].replace('/', '')
        # make sure the queried endpoint exists, if not throw an informative error
        if endpoint not in self.rest_schemes.keys():
            av = [k for k in self.rest_schemes.keys() if not k.startswith('_') and k]
            raise ValueError('REST endpoint "' + endpoint + '" does not exist. Available ' +
                             'endpoints are \n       ' + '\n       '.join(av))
        endpoint_scheme = self.rest_schemes[endpoint]
        # on a filter request, override the default action parameter
        if '?' in url:
            action = 'list'
        # if action is None, list available actions for the required endpoint
        if not action:
            pprint(list(endpoint_scheme.keys()))
            return
        # make sure the the desired action exists, if not throw an informative error
        if action not in endpoint_scheme:
            raise ValueError('Action "' + action + '" for REST endpoint "' + endpoint + '" does ' +
                             'not exist. Available actions are: ' +
                             '\n       ' + '\n       '.join(endpoint_scheme.keys()))
        # the actions below require an id in the URL, warn and help the user
        if action in ['read', 'update', 'partial_update', 'delete'] and not id:
            _logger.warning('REST action "' + action + '" requires an ID in the URL: ' +
                            endpoint_scheme[action]['url'])
            return
        # the actions below require a data dictionary, warn and help the user with fields list
        if action in ['create', 'update', 'partial_update'] and not data:
            pprint(endpoint_scheme[action]['fields'])
            for act in endpoint_scheme[action]['fields']:
                print("'" + act['name'] + "': ...,")
            _logger.warning('REST action "' + action + '" requires a data dict with above keys')
            return

        # clobber=True means remote request always made, expires=True means response is not cached
        cache_args = {'clobber': no_cache, 'expires': no_cache}
        if action == 'list':
            # list doesn't require id nor
            assert endpoint_scheme[action]['action'] == 'get'
            # add to url data if it is a string
            if id:
                # this is a special case of the list where we query a uuid. Usually read is better
                if 'django' in kwargs.keys():
                    kwargs['django'] = kwargs['django'] + ','
                else:
                    kwargs['django'] = ""
                kwargs['django'] = f"{kwargs['django']}pk,{id}"
            # otherwise, look for a dictionary of filter terms
            if kwargs:
                # Convert all lists in query params to comma separated list
                query_params = {k: ','.join(map(str, ensure_list(v))) for k, v in kwargs.items()}
                url = update_url_params(url, query_params)
            return self.get('/' + url, **cache_args)
        if not isinstance(id, str) and id is not None:
            id = str(id)  # e.g. may be uuid.UUID
        if action == 'read':
            assert (endpoint_scheme[action]['action'] == 'get')
            return self.get('/' + endpoint + '/' + id.split('/')[-1], **cache_args)
        elif action == 'create':
            assert (endpoint_scheme[action]['action'] == 'post')
            return self.post('/' + endpoint, data=data, files=files)
        elif action == 'delete':
            assert (endpoint_scheme[action]['action'] == 'delete')
            return self.delete('/' + endpoint + '/' + id.split('/')[-1])
        elif action == 'partial_update':
            assert (endpoint_scheme[action]['action'] == 'patch')
            return self.patch('/' + endpoint + '/' + id.split('/')[-1], data=data, files=files)
        elif action == 'update':
            assert (endpoint_scheme[action]['action'] == 'put')
            return self.put('/' + endpoint + '/' + id.split('/')[-1], data=data, files=files)

    # JSON field interface convenience methods
    def _check_inputs(self, endpoint: str) -> None:
        # make sure the queried endpoint exists, if not throw an informative error
        if endpoint not in self.rest_schemes.keys():
            av = [k for k in self.rest_schemes.keys() if not k.startswith('_') and k]
            raise ValueError('REST endpoint "' + endpoint + '" does not exist. Available ' +
                             'endpoints are \n       ' + '\n       '.join(av))
        return

    def json_field_write(
            self,
            endpoint: str = None,
            uuid: str = None,
            field_name: str = None,
            data: dict = None
    ) -> dict:
        """json_field_write [summary]
        Write data to WILL NOT CHECK IF DATA EXISTS
        NOTE: Destructive write!

        Parameters
        ----------
        endpoint : str, None
            Valid alyx endpoint, defaults to None
        uuid : str, uuid.UUID, None
            UUID or lookup name for endpoint
        field_name : str, None
            Valid json field name, defaults to None
        data : dict, None
            Data to write to json field, defaults to None

        Returns
        -------
        Written data dict
        """
        self._check_inputs(endpoint)
        # Prepare data to patch
        patch_dict = {field_name: data}
        # Upload new extended_qc to session
        ret = self.rest(endpoint, "partial_update", id=uuid, data=patch_dict)
        return ret[field_name]

    def json_field_update(
            self,
            endpoint: str = None,
            uuid: str = None,
            field_name: str = 'json',
            data: dict = None
    ) -> dict:
        """json_field_update
        Non destructive update of json field of endpoint for object
        Will update the field_name of the object with pk = uuid of given endpoint
        If data has keys with the same name of existing keys it will squash the old
        values (uses the dict.update() method)

        Parameters
        ----------
        endpoint : str
            Alyx REST endpoint to hit
        uuid : str, uuid.UUID
            UUID or lookup name of object
        field_name : str
            Name of the json field
        data : dict
            A dictionary with fields to be updated

        Returns
        -------
        New patched json field contents as dict

        Examples
        --------
        one.alyx.json_field_update("sessions", "eid_str", "extended_qc", {"key": value})
        """
        self._check_inputs(endpoint)
        # Load current json field contents
        current = self.rest(endpoint, "read", id=uuid)[field_name]
        if current is None:
            current = {}

        if not isinstance(current, dict):
            _logger.warning(
                f"Current json field {field_name} does not contains a dict, aborting update"
            )
            return current

        # Patch current dict with new data
        current.update(data)
        # Prepare data to patch
        patch_dict = {field_name: current}
        # Upload new extended_qc to session
        ret = self.rest(endpoint, "partial_update", id=uuid, data=patch_dict)
        return ret[field_name]

    def json_field_remove_key(
            self,
            endpoint: str = None,
            uuid: str = None,
            field_name: str = 'json',
            key: str = None
    ) -> Optional[dict]:
        """json_field_remove_key
        Will remove inputted key from json field dict and reupload it to Alyx.
        Needs endpoint, uuid and json field name

        :param endpoint: endpoint to hit, defaults to None
        :type endpoint: str, optional
        :param uuid: uuid or lookup name for endpoint
        :type uuid: str, optional
        :param field_name: json field name of object, defaults to None
        :type field_name: str, optional
        :param key: key name of dictionary inside object, defaults to None
        :type key: str, optional
        :return: returns new content of json field
        :rtype: dict
        """
        self._check_inputs(endpoint)
        current = self.rest(endpoint, "read", id=uuid)[field_name]
        # If no contents, cannot remove key, return
        if current is None:
            return current
        # if contents are not dict, cannot remove key, return contents
        if isinstance(current, str):
            _logger.warning(f"Cannot remove key {key} content of json field is of type str")
            return None
        # If key not present in contents of json field cannot remove key, return contents
        if current.get(key, None) is None:
            _logger.warning(
                f"{key}: Key not found in endpoint {endpoint} field {field_name}"
            )
            return current
        _logger.info(f"Removing key from dict: '{key}'")
        current.pop(key)
        # Re-write contents without removed key
        written = self.json_field_write(
            endpoint=endpoint, uuid=uuid, field_name=field_name, data=current
        )
        return written

    def json_field_delete(
            self, endpoint: str = None, uuid: str = None, field_name: str = None
    ) -> None:
        self._check_inputs(endpoint)
        _ = self.rest(endpoint, "partial_update", id=uuid, data={field_name: None})
        return _[field_name]

    def clear_rest_cache(self):
        for file in one.params.get_rest_dir(self.base_url).glob('*'):
            file.unlink()
