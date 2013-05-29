from __future__ import absolute_import

from base64 import b64decode, b64encode
from contextlib import contextmanager
import sys
from urlparse import urlparse, urlunparse
try:
    import cPickle as pickle
except ImportError:                     # pragma: no cover
    import pickle

from trac.config import BoolOption, OrderedExtensionsOption
from trac.core import implements, TracError
from trac.db.util import ConnectionWrapper
from trac.env import IEnvironmentSetupParticipant
from trac.web.api import RequestDone

import openid.consumer.consumer
from openid.consumer.consumer import SUCCESS, FAILURE, CANCEL, SETUP_NEEDED

from openid import oidutil
import openid.store.memstore
import openid.store.sqlstore

from authopenid.api import (
    IOpenIDExtensionProvider,
    OpenIDIdentifier,
    AuthenticationFailed,
    AuthenticationCancelled,
    SetupNeeded,
    )
from authopenid.compat import Component, TransactionContextManager
from authopenid.interfaces import IOpenIDConsumer
from authopenid.util import get_db_scheme, table_exists

# XXX: It looks like python-openid is going to switch to using the
# stock logging module.  We'll need to detect when that happens.
@contextmanager
def openid_logging_to(log):
    """ Capture logging from python-openid to the trac log.
    """
    def log_to_trac_log(message, level=0):
        # XXX: What level to log at?
        # The level argument is unused python-openid.  Log messages
        # generated by python-openid seem to range from INFO to ERROR
        # severity, but there is no good way to distinguish which is which.
        log.warning("%s", message)

    save_log, oidutil.log = oidutil.log, log_to_trac_log
    try:
        yield
    finally:
        oidutil.log = save_log

def _session_mutator(method):
    def wrapped(self, *args):
        rv = method(self, *args)
        self.save()
        return rv
    try:
        wrapped.__name__ = method.__name__
    except:                             # pragma: no cover
        pass
    return wrapped

class PickleSession(dict):
    """ A session dict that can store any kind of object.

    (The trac req.session can only store ``unicode`` values.)
    """

    def __init__(self, req, skey):
        self.req = req
        self.skey = skey
        try:
            data = b64decode(req.session[self.skey])
            self.update(pickle.loads(data))
        except (KeyError, TypeError, pickle.UnpicklingError):
            pass

    def save(self):
        session = self.req.session
        if len(self) > 0:
            data = pickle.dumps(dict(self), pickle.HIGHEST_PROTOCOL)
            session[self.skey] = b64encode(data)
        elif self.skey in session:
            del session[self.skey]

    __setitem__ = _session_mutator(dict.__setitem__)
    __delitem__ = _session_mutator(dict.__delitem__)
    clear = _session_mutator(dict.clear)
    pop = _session_mutator(dict.pop)
    popitem = _session_mutator(dict.popitem)
    setdefault = _session_mutator(dict.setdefault)
    update = _session_mutator(dict.update)

class openid_store(object):
    """ Context manager for adapting trac db to python-openid store

    Usage::

        with openid_store(env) as store:
            # do stuff which requires an ``python-openid store.

    The context manager will take care of choosing the appropriate
    ``OpenIDStore`` implementation, and of starting and committing
    (or rolling back on exception) a db transaction.

    If you are in the midst of a transaction and already have a trac
    connection object, then you can explicitly pass the connection::

        with openid_store(env, db) as store:
            # do stuff which requires an ``python-openid store.

    In this case, no db transaction management will be done.

    """
    STORE_CLASSES = {
        'sqlite': openid.store.sqlstore.SQLiteStore,
        'mysql': openid.store.sqlstore.MySQLStore,
        'postgres': openid.store.sqlstore.PostgreSQLStore,
        }

    def __init__(self, env, db=None):
        self.env = env
        self.db = db

        scheme = get_db_scheme(env)
        self.store_class = self.STORE_CLASSES.get(scheme)

    def __enter__(self):
        self._exit = lambda type_, value, tb: None # dummy exit fn

        if self.store_class is None:
            # no store class for database type, punt...
            return openid.store.memstore.MemoryStore()

        conn = self.db
        if conn is None:
            tm = TransactionContextManager(self.env)
            self._exit = tm.__exit__
            conn = tm.__enter__()

        try:
            # Get the raw connection object
            # (See http://trac.edgewall.org/ticket/7849)
            while isinstance(conn, ConnectionWrapper):
                conn = conn.cnx
            return self.store_class(conn)
        except:
            self._exit(*sys.exc_info())
            raise

    def __exit__(self, type_, value, tb):
        self._exit(type_, value, tb)


class OpenIDConsumer(Component):

    implements(IOpenIDConsumer, IEnvironmentSetupParticipant)


    absolute_trust_root = BoolOption(
        'openid', 'absolute_trust_root', 'true',
        doc="""Does OpenID realm include the whole site, or just the project

        If true (the default) then a url to the root of the whole site
        will be sent for the OpenID realm.  Thus when a user approves
        authentication, he will be approving it for all trac projects
        on the site.

        Set to false to send a realm which only includes the current trac
        project.
        """)

    openid_extension_providers = OrderedExtensionsOption(
        'openid', 'openid_extension_providers', IOpenIDExtensionProvider)

    consumer_class = openid.consumer.consumer.Consumer # testing

    consumer_skey = 'openid_session_data'

    schema_version_key = 'authopenid.openid_store_version'

    # IOpenIDConsumer methods

    def begin(self, req, identifier, return_to,
              trust_root=None, immediate=False):

        log = self.env.log

        if trust_root is None:
            trust_root = self._get_trust_root(req)

        session = PickleSession(req, self.consumer_skey)
        with openid_store(self.env) as store:
            with openid_logging_to(log):
                consumer = self.consumer_class(session, store)
                # NB: raises openid.consumer.discover.DiscoveryFailure
                # FIXME: (and maybe ProtocolError?)
                auth_request = consumer.begin(identifier)

                for provider in self.openid_extension_providers:
                    provider.add_to_auth_request(req, auth_request)

                if auth_request.shouldSendRedirect():
                    redirect_url = auth_request.redirectURL(
                        trust_root, return_to, immediate=immediate)
                    log.debug('Redirecting to: %s' % redirect_url)
                    req.redirect(redirect_url)  # noreturn (raises RequestDone)
                else:
                    # return an auto-submit form
                    form_html = auth_request.htmlMarkup(
                        trust_root, return_to, immediate=immediate)
                    req.send(form_html, 'text/html')
                raise RequestDone

    def complete(self, req, current_url=None):
        if current_url is None:
            current_url = req.abs_href(req.path_info)

        session = PickleSession(req, self.consumer_skey)
        with openid_store(self.env) as store:
            with openid_logging_to(self.env.log):
                consumer = self.consumer_class(session, store)
                response = consumer.complete(req.args, current_url)

                if response.status != SETUP_NEEDED:
                    session.clear()

                if response.status == FAILURE:
                    raise AuthenticationFailed(
                        response.message, response.identity_url)
                elif response.status == CANCEL:
                    raise AuthenticationCancelled()
                elif response.status == SETUP_NEEDED:
                    raise SetupNeeded(response.setup_url)
                assert response.status == SUCCESS

                if response.endpoint.canonicalID:
                    # Authorize i-name users by their canonicalID,
                    # rather than their human-friendly identifiers.
                    # That way their account with you is not
                    # compromised if their i-name registration expires
                    # and is bought by someone else.

                    claimed_identifier = response.endpoint.canonicalID
                else:
                    claimed_identifier = response.identity_url

                identifier = OpenIDIdentifier(claimed_identifier)

                for provider in self.openid_extension_providers:
                    provider.parse_response(response, identifier)

                return identifier

    def _get_trust_root(self, req):
        root = urlparse(req.abs_href() + '/')
        assert root.scheme and root.netloc
        path = root.path
        if self.absolute_trust_root:
            path = '/'
        else:
            path = root.path
        if not path.endswith('/'):
            path += '/'
        return urlunparse((root.scheme, root.netloc, path) + (None,) * 3)

    # IEnvironmentSetupParticipant methods

    def environment_created(self):
        with self.env.db_transaction as db:
            self.upgrade_environment(db)

    def environment_needs_upgrade(self, db):
        have = self._get_schema_version()
        if have is None:
            return True
        with openid_store(self.env, db) as store:
            want = 1 if hasattr(store, 'createTables') else 0

        if have > want:
            raise TracError("Downgrading unsupported: "
                            "openid store version is %d, we want %d"
                            % (have, want))
        return have < want

    def upgrade_environment(self, db):
        version = orig = self._get_schema_version()
        if version is None:
            # create initial system table entry
            version = 0
            db("INSERT INTO system (name, value) VALUES (%s, %s)",
               (self.schema_version_key, str(version)))
            # XXX: store.createTables() starts be issuing a rollback
            db.commit()
        assert 0 <= version < 2

        # Be careful: if version was None, we may still have the tables
        # left from before we started recording schema version in the
        # system table.
        #
        if version < 1:
            if not table_exists(self.env, 'oid_associations'):
                with openid_store(self.env, db) as store:
                    if hasattr(store, 'createTables'):
                        store.createTables()
            version = 1

        db("UPDATE system SET value=%s WHERE name=%s",
           (str(version), self.schema_version_key))

        self.log.info("Upgraded openid store schema from %r to %d"
                      % (orig, version))

    def _get_schema_version(self):
        rows = self.env.db_query(" SELECT value FROM system WHERE name=%s",
                                 (self.schema_version_key,))
        return int(rows[0][0]) if rows else None
