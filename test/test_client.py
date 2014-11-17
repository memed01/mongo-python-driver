# Copyright 2009-2014 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test the mongo_client module."""

import contextlib
import datetime
import multiprocessing
import os
import threading
import socket
import sys
import time
import traceback
import warnings

sys.path[0:0] = [""]

from bson.py3compat import thread, u
from bson.son import SON
from bson.tz_util import utc
from pymongo.mongo_client import MongoClient
from pymongo.database import Database
from pymongo.pool import SocketInfo
from pymongo import auth, message
from pymongo.errors import (AutoReconnect,
                            ConfigurationError,
                            ConnectionFailure,
                            InvalidName,
                            OperationFailure,
                            CursorNotFound,
                            NetworkTimeout,
                            InvalidURI)
from pymongo.read_preferences import ReadPreference
from pymongo.server_selectors import (any_server_selector,
                                      writable_server_selector)
from pymongo.server_type import SERVER_TYPE
from test import (client_context,
                  client_knobs,
                  host,
                  pair,
                  port,
                  SkipTest,
                  unittest,
                  IntegrationTest,
                  db_pwd,
                  db_user,
                  MockClientTest)
from test.pymongo_mocks import MockClient
from test.utils import (assertRaisesExactly,
                        delay,
                        ignore_deprecations,
                        remove_all_users,
                        server_is_master_with_slave,
                        TestRequestMixin,
                        get_pool,
                        one,
                        connected,
                        wait_until,
                        rs_or_single_client,
                        rs_or_single_client_noauth,
                        lazy_client_trial,
                        NTHREADS)


class ClientUnitTest(unittest.TestCase, TestRequestMixin):
    """MongoClient tests that don't require a server."""

    @classmethod
    def setUpClass(cls):
        cls.client = MongoClient(host, port, connect=False)

    def test_keyword_arg_defaults(self):
        client = MongoClient(socketTimeoutMS=None,
                             connectTimeoutMS=20000,
                             waitQueueTimeoutMS=None,
                             waitQueueMultiple=None,
                             socketKeepAlive=False,
                             replicaSet=None,
                             read_preference=ReadPreference.PRIMARY,
                             ssl=False,
                             ssl_keyfile=None,
                             ssl_certfile=None,
                             ssl_cert_reqs=0,  # ssl.CERT_NONE
                             ssl_ca_certs=None,
                             connect=False)

        options = client._MongoClient__options
        pool_opts = options.pool_options
        self.assertEqual(None, pool_opts.socket_timeout)
        # socket.Socket.settimeout takes a float in seconds
        self.assertEqual(20.0, pool_opts.connect_timeout)
        self.assertEqual(None, pool_opts.wait_queue_timeout)
        self.assertEqual(None, pool_opts.wait_queue_multiple)
        self.assertFalse(pool_opts.socket_keepalive)
        self.assertEqual(None, pool_opts.ssl_context)
        self.assertEqual(None, options.replica_set_name)
        self.assertEqual(ReadPreference.PRIMARY, client.read_preference)

    def test_types(self):
        self.assertRaises(TypeError, MongoClient, 1)
        self.assertRaises(TypeError, MongoClient, 1.14)
        self.assertRaises(TypeError, MongoClient, "localhost", "27017")
        self.assertRaises(TypeError, MongoClient, "localhost", 1.14)
        self.assertRaises(TypeError, MongoClient, "localhost", [])

        self.assertRaises(ConfigurationError, MongoClient, [])

    def test_get_db(self):
        def make_db(base, name):
            return base[name]

        self.assertRaises(InvalidName, make_db, self.client, "")
        self.assertRaises(InvalidName, make_db, self.client, "te$t")
        self.assertRaises(InvalidName, make_db, self.client, "te.t")
        self.assertRaises(InvalidName, make_db, self.client, "te\\t")
        self.assertRaises(InvalidName, make_db, self.client, "te/t")
        self.assertRaises(InvalidName, make_db, self.client, "te st")

        self.assertTrue(isinstance(self.client.test, Database))
        self.assertEqual(self.client.test, self.client["test"])
        self.assertEqual(self.client.test, Database(self.client, "test"))

    def test_getattr(self):
        self.assertTrue(isinstance(self.client['_does_not_exist'], Database))

        with self.assertRaises(AttributeError) as context:
            self.client._does_not_exist

        # Message should be:
        # "AttributeError: MongoClient has no attribute '_does_not_exist'. To
        # access the _does_not_exist database, use client['_does_not_exist']".
        self.assertIn("has no attribute '_does_not_exist'",
                      str(context.exception))

    def test_iteration(self):
        def iterate():
            [a for a in self.client]

        self.assertRaises(TypeError, iterate)

    def test_get_default_database(self):
        c = MongoClient("mongodb://%s:%d/foo" % (host, port), connect=False)
        self.assertEqual(Database(c, 'foo'), c.get_default_database())

    def test_get_default_database_error(self):
        # URI with no database.
        c = MongoClient("mongodb://%s:%d/" % (host, port), connect=False)
        self.assertRaises(ConfigurationError, c.get_default_database)

    def test_get_default_database_with_authsource(self):
        # Ensure we distinguish database name from authSource.
        uri = "mongodb://%s:%d/foo?authSource=src" % (host, port)
        c = MongoClient(uri, connect=False)
        self.assertEqual(Database(c, 'foo'), c.get_default_database())


class TestClient(IntegrationTest, TestRequestMixin):

    def test_constants(self):
        # Set bad defaults.
        MongoClient.HOST = "somedomainthatdoesntexist.org"
        MongoClient.PORT = 123456789
        with client_knobs(server_wait_time=0.01):
            with self.assertRaises(AutoReconnect):
                connected(MongoClient())

        # Override the defaults. No error.
        connected(MongoClient(host, port))

        # Set good defaults.
        MongoClient.HOST = host
        MongoClient.PORT = port

        # No error.
        connected(MongoClient())

    def test_init_disconnected(self):
        c = rs_or_single_client(connect=False)

        self.assertIsInstance(c.is_primary, bool)
        self.assertIsInstance(c.is_mongos, bool)
        self.assertIsInstance(c.max_pool_size, int)
        self.assertIsInstance(c.nodes, frozenset)

        with ignore_deprecations():
            self.assertEqual(dict, c.get_document_class())

        self.assertIsInstance(c.tz_aware, bool)
        self.assertIsInstance(c.max_bson_size, int)
        self.assertIsInstance(c.min_wire_version, int)
        self.assertIsInstance(c.max_wire_version, int)
        self.assertIsInstance(c.max_write_batch_size, int)
        self.assertEqual(None, c.host)
        self.assertEqual(None, c.port)
        self.assertFalse(c.primary)
        self.assertFalse(c.secondaries)

        c.pymongo_test.command('ismaster')  # Auto-connect.
        self.assertEqual(host, c.host)
        self.assertEqual(port, c.port)

        if client_context.version.at_least(2, 5, 4, -1):
            self.assertTrue(c.max_wire_version > 0)
        else:
            self.assertEqual(c.max_wire_version, 0)
        self.assertTrue(c.min_wire_version >= 0)

        bad_host = "somedomainthatdoesntexist.org"
        with client_knobs(server_wait_time=0.01):
            c = MongoClient(bad_host, port)
            self.assertRaises(ConnectionFailure, c.pymongo_test.test.find_one)

    def test_init_disconnected_with_auth(self):
        uri = "mongodb://user:pass@somedomainthatdoesntexist"
        with client_knobs(server_wait_time=0.01):
            c = MongoClient(uri)
            self.assertRaises(ConnectionFailure, c.pymongo_test.test.find_one)

    def test_equality(self):
        c = connected(rs_or_single_client())
        self.assertEqual(client_context.rs_or_standalone_client, c)

        # Explicitly test inequality
        self.assertFalse(client_context.rs_or_standalone_client != c)

    def test_host_w_port(self):
        with client_knobs(server_wait_time=0.01):
            with self.assertRaises(AutoReconnect):
                connected(MongoClient("%s:1234567" % host, connectTimeoutMS=1))

    def test_repr(self):
        # Making host a str avoids the 'u' prefix in Python 2, so the repr is
        # the same in Python 2 and 3.
        self.assertEqual(repr(MongoClient(str(host), port)),
                         "MongoClient('%s', %d)" % (host, port))

    @client_context.require_replica_set
    def test_repr_replica_set(self):
        # Like MongoClient(["localhost:27017", "localhost:27018"]).
        self.assertIn("MongoClient([", repr(self.client))
        for node in client_context.nodes:
            self.assertIn("%s:%d" % node, repr(self.client))

    def test_getters(self):
        self.assertEqual(client_context.client.host, host)
        self.assertEqual(client_context.client.port, port)
        self.assertEqual(client_context.nodes, self.client.nodes)

    def test_database_names(self):
        self.client.pymongo_test.test.save({"dummy": u("object")})
        self.client.pymongo_test_mike.test.save({"dummy": u("object")})

        dbs = self.client.database_names()
        self.assertTrue("pymongo_test" in dbs)
        self.assertTrue("pymongo_test_mike" in dbs)

    def test_drop_database(self):
        self.assertRaises(TypeError, self.client.drop_database, 5)
        self.assertRaises(TypeError, self.client.drop_database, None)

        self.client.pymongo_test.test.save({"dummy": u("object")})
        self.client.pymongo_test2.test.save({"dummy": u("object")})
        dbs = self.client.database_names()
        self.assertIn("pymongo_test", dbs)
        self.assertIn("pymongo_test2", dbs)
        self.client.drop_database("pymongo_test")
        self.client.drop_database(self.client.pymongo_test2)

        raise SkipTest("This test often fails due to SERVER-2329")

        dbs = self.client.database_names()
        self.assertNotIn("pymongo_test", dbs)
        self.assertNotIn("pymongo_test2", dbs)

    def test_disconnect(self):
        coll = self.client.pymongo_test.bar

        self.client.disconnect()
        self.client.disconnect()

        coll.count()

        self.client.disconnect()
        self.client.disconnect()

        coll.count()

    def test_bad_uri(self):
        with self.assertRaises(InvalidURI):
            MongoClient("http://localhost")

    @client_context.require_auth
    def test_auth_from_uri(self):
        self.client.admin.add_user("admin", "pass", roles=["root"])
        try:
            self.client.pymongo_test.add_user(
                "user", "pass", roles=['userAdmin', 'readWrite'])

            with self.assertRaises(OperationFailure):
                connected(rs_or_single_client(
                    "mongodb://a:b@%s:%d" % (host, port)))

            # No error.
            connected(rs_or_single_client_noauth(
                "mongodb://admin:pass@%s:%d" % (host, port)))

            # Wrong database.
            uri = "mongodb://admin:pass@%s:%d/pymongo_test" % (host, port)
            with self.assertRaises(OperationFailure):
                connected(rs_or_single_client(uri))

            # No error.
            connected(rs_or_single_client_noauth(
                "mongodb://user:pass@%s:%d/pymongo_test" % (host, port)))

            # Auth with lazy connection.
            rs_or_single_client(
                "mongodb://user:pass@%s:%d/pymongo_test" % (host, port),
                connect=False).pymongo_test.test.find_one()

            # Wrong password.
            bad_client = rs_or_single_client(
                "mongodb://user:wrong@%s:%d/pymongo_test" % (host, port),
                connect=False)

            self.assertRaises(OperationFailure,
                              bad_client.pymongo_test.test.find_one)

        finally:
            # Clean up.
            remove_all_users(self.client.pymongo_test)
            self.client.admin.remove_user('admin')

    @client_context.require_auth
    def test_multiple_logins(self):
        self.client.pymongo_test.add_user('user1', 'pass', roles=['readWrite'])
        self.client.pymongo_test.add_user('user2', 'pass', roles=['readWrite'])

        try:
            client = rs_or_single_client_noauth(
                "mongodb://user1:pass@%s:%d/pymongo_test" % (host, port))

            client.pymongo_test.test.find_one()
            with self.assertRaises(OperationFailure):
                # Can't log in to the same database with multiple users.
                client.pymongo_test.authenticate('user2', 'pass')

            client.pymongo_test.test.find_one()
            client.pymongo_test.logout()
            with self.assertRaises(OperationFailure):
                client.pymongo_test.test.find_one()

            client.pymongo_test.authenticate('user2', 'pass')
            client.pymongo_test.test.find_one()

            with self.assertRaises(OperationFailure):
                client.pymongo_test.authenticate('user1', 'pass')

            client.pymongo_test.test.find_one()

        finally:
            remove_all_users(self.client.pymongo_test)

    @client_context.require_auth
    def test_lazy_auth_raises_operation_failure(self):
        lazy_client = rs_or_single_client(
            "mongodb://user:wrong@%s/pymongo_test" % host, connect=False)

        assertRaisesExactly(
            OperationFailure, lazy_client.test.collection.find_one)

    def test_unix_socket(self):
        if not hasattr(socket, "AF_UNIX"):
            raise SkipTest("UNIX-sockets are not supported on this system")

        mongodb_socket = '/tmp/mongodb-27017.sock'
        if not os.access(mongodb_socket, os.R_OK):
            raise SkipTest("Socket file is not accessible")

        if client_context.auth_enabled:
            uri = "mongodb://%s:%s@%s" % (db_user, db_pwd, mongodb_socket)
        else:
            uri = "mongodb://%s" % mongodb_socket

        # Confirm we can do operations via the socket.
        client = MongoClient(uri)
        client.pymongo_test.test.save({"dummy": "object"})
        dbs = client.database_names()
        self.assertTrue("pymongo_test" in dbs)

        # Confirm it fails with a missing socket.
        with client_knobs(server_wait_time=0.1):
            self.assertRaises(
                ConnectionFailure,
                connected, MongoClient("mongodb:///tmp/non-existent.sock"))

    def test_fork(self):
        # Test using a client before and after a fork.
        if sys.platform == "win32":
            raise SkipTest("Can't fork on windows")

        db = self.client.pymongo_test

        # Ensure a socket is opened before the fork.
        db.test.find_one()

        def f(pipe):
            try:
                servers = self.client._topology.select_servers(
                    any_server_selector)

                # In child, only the thread that called fork() is alive.
                assert not any(s._monitor._thread.is_alive()
                               for s in servers)

                db.test.find_one()

                wait_until(
                    lambda: all(s._monitor._thread.is_alive() for s in servers),
                    "restart monitor threads")
            except:
                traceback.print_exc()  # Aid debugging.
                pipe.send(True)

        parent_pipe, child_pipe = multiprocessing.Pipe()
        p = multiprocessing.Process(target=f, args=(child_pipe,))
        p.start()
        p.join(10)
        child_pipe.close()

        # Pipe will only have data if the child process failed.
        try:
            parent_pipe.recv()
            self.fail()
        except EOFError:
            pass

    def test_document_class(self):
        c = self.client
        db = c.pymongo_test
        db.test.insert({"x": 1})

        self.assertEqual(dict, c.document_class)
        self.assertTrue(isinstance(db.test.find_one(), dict))
        self.assertFalse(isinstance(db.test.find_one(), SON))

        c = rs_or_single_client(document_class=SON)
        db = c.pymongo_test

        self.assertEqual(SON, c.document_class)
        self.assertTrue(isinstance(db.test.find_one(), SON))
        self.assertFalse(isinstance(db.test.find_one(as_class=dict), SON))

        # document_class is read-only in PyMongo 3.0.
        with self.assertRaises(AttributeError):
            c.document_class = dict

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            self.assertRaises(DeprecationWarning, c.get_document_class)

    def test_timeouts(self):
        client = rs_or_single_client(connectTimeoutMS=10500)
        self.assertEqual(10.5, get_pool(client).opts.connect_timeout)
        client = rs_or_single_client(socketTimeoutMS=10500)
        self.assertEqual(10.5, get_pool(client).opts.socket_timeout)

    def test_socket_timeout_ms_validation(self):
        c = rs_or_single_client(socketTimeoutMS=10 * 1000)
        self.assertEqual(10, get_pool(c).opts.socket_timeout)

        c = connected(rs_or_single_client(socketTimeoutMS=None))
        self.assertEqual(None, get_pool(c).opts.socket_timeout)

        self.assertRaises(ConfigurationError,
                          rs_or_single_client, socketTimeoutMS=0)

        self.assertRaises(ConfigurationError,
                          rs_or_single_client, socketTimeoutMS=-1)

        self.assertRaises(ConfigurationError,
                          rs_or_single_client, socketTimeoutMS=1e10)

        self.assertRaises(ConfigurationError,
                          rs_or_single_client, socketTimeoutMS='foo')

    def test_socket_timeout(self):
        no_timeout = self.client
        timeout_sec = 1
        timeout = rs_or_single_client(socketTimeoutMS=1000 * timeout_sec)

        no_timeout.pymongo_test.drop_collection("test")
        no_timeout.pymongo_test.test.insert({"x": 1})

        # A $where clause that takes a second longer than the timeout
        where_func = delay(timeout_sec + 1)

        def get_x(db):
            doc = next(db.test.find().where(where_func))
            return doc["x"]
        self.assertEqual(1, get_x(no_timeout.pymongo_test))
        self.assertRaises(NetworkTimeout, get_x, timeout.pymongo_test)

    def test_waitQueueTimeoutMS(self):
        client = rs_or_single_client(waitQueueTimeoutMS=2000)
        self.assertEqual(get_pool(client).opts.wait_queue_timeout, 2)

    def test_waitQueueMultiple(self):
        client = rs_or_single_client(max_pool_size=3, waitQueueMultiple=2)
        pool = get_pool(client)
        self.assertEqual(pool.opts.wait_queue_multiple, 2)
        self.assertEqual(pool._socket_semaphore.waiter_semaphore.counter, 6)

    def test_socketKeepAlive(self):
        client = rs_or_single_client(socketKeepAlive=True)
        self.assertTrue(get_pool(client).opts.socket_keepalive)

    def test_tz_aware(self):
        self.assertRaises(ConfigurationError, MongoClient, tz_aware='foo')

        aware = rs_or_single_client(tz_aware=True)
        naive = self.client
        aware.pymongo_test.drop_collection("test")

        now = datetime.datetime.utcnow()
        aware.pymongo_test.test.insert({"x": now})

        self.assertEqual(None, naive.pymongo_test.test.find_one()["x"].tzinfo)
        self.assertEqual(utc, aware.pymongo_test.test.find_one()["x"].tzinfo)
        self.assertEqual(
                aware.pymongo_test.test.find_one()["x"].replace(tzinfo=None),
                naive.pymongo_test.test.find_one()["x"])

    @client_context.require_ipv6
    def test_ipv6(self):
        if client_context.auth_enabled:
            auth_str = "%s:%s@" % (db_user, db_pwd)
        else:
            auth_str = ""

        uri = "mongodb://%s[::1]:%d" % (auth_str, port)
        if client_context.is_rs:
            uri += '/?replicaSet=' + client_context.replica_set_name

        client = rs_or_single_client_noauth(uri)
        client.pymongo_test.test.save({"dummy": u("object")})
        client.pymongo_test_bernie.test.save({"dummy": u("object")})

        dbs = client.database_names()
        self.assertTrue("pymongo_test" in dbs)
        self.assertTrue("pymongo_test_bernie" in dbs)

    @client_context.require_no_mongos
    def test_fsync_lock_unlock(self):
        if (not client_context.version.at_least(2, 0) and
                client_context.auth_enabled):
            raise SkipTest('Requires server >= 2.0 to test with auth')
        if (server_is_master_with_slave(client_context.client) and
                client_context.version.at_least(2, 3, 0)):
            raise SkipTest('SERVER-7714')

        self.assertFalse(self.client.is_locked)
        # async flushing not supported on windows...
        if sys.platform not in ('cygwin', 'win32'):
            self.client.fsync(async=True)
            self.assertFalse(self.client.is_locked)
        self.client.fsync(lock=True)
        self.assertTrue(self.client.is_locked)
        locked = True
        self.client.unlock()
        for _ in range(5):
            locked = self.client.is_locked
            if not locked:
                break
            time.sleep(1)
        self.assertFalse(locked)

    def test_contextlib(self):
        client = rs_or_single_client()
        client.pymongo_test.drop_collection("test")
        client.pymongo_test.test.insert({"foo": "bar"})

        # The socket used for the previous commands has been returned to the
        # pool
        self.assertEqual(1, len(get_pool(client).sockets))

        with contextlib.closing(client):
            self.assertEqual("bar", client.pymongo_test.test.find_one()["foo"])
            self.assertEqual(1, len(get_pool(client).sockets))
        self.assertEqual(0, len(get_pool(client).sockets))

        with client as client:
            self.assertEqual("bar", client.pymongo_test.test.find_one()["foo"])
        self.assertEqual(0, len(get_pool(client).sockets))

    def test_with_start_request(self):
        pool = get_pool(self.client)

        # No request started
        self.assertNoRequest(pool)
        self.assertDifferentSock(pool)

        # Start a request
        request_context_mgr = self.client.start_request()
        self.assertTrue(
            isinstance(request_context_mgr, object)
        )

        self.assertNoSocketYet(pool)
        self.assertSameSock(pool)
        self.assertRequestSocket(pool)

        # End request
        request_context_mgr.__exit__(None, None, None)
        self.assertNoRequest(pool)
        self.assertDifferentSock(pool)

        # Test the 'with' statement
        with self.client.start_request() as request:
            self.assertEqual(self.client, request.connection)
            self.assertNoSocketYet(pool)
            self.assertSameSock(pool)
            self.assertRequestSocket(pool)

        # Request has ended
        self.assertNoRequest(pool)
        self.assertDifferentSock(pool)

    def test_request_threads(self):
        client = self.client
        pool = get_pool(client)
        self.assertNotInRequestAndDifferentSock(client, pool)

        started_request, ended_request = threading.Event(), threading.Event()
        checked_request = threading.Event()
        thread_done = [False]

        # Starting a request in one thread doesn't put the other thread in a
        # request
        def f():
            self.assertNotInRequestAndDifferentSock(client, pool)
            client.start_request()
            self.assertInRequestAndSameSock(client, pool)
            started_request.set()
            checked_request.wait()
            checked_request.clear()
            self.assertInRequestAndSameSock(client, pool)
            client.end_request()
            self.assertNotInRequestAndDifferentSock(client, pool)
            ended_request.set()
            checked_request.wait()
            thread_done[0] = True

        t = threading.Thread(target=f)
        t.setDaemon(True)
        t.start()
        # It doesn't matter in what order the main thread or t initially get
        # to started_request.set() / wait(); by waiting here we ensure that t
        # has called client.start_request() before we assert on the next line.
        started_request.wait()
        self.assertNotInRequestAndDifferentSock(client, pool)
        checked_request.set()
        ended_request.wait()
        self.assertNotInRequestAndDifferentSock(client, pool)
        checked_request.set()
        t.join()
        self.assertNotInRequestAndDifferentSock(client, pool)
        self.assertTrue(thread_done[0], "Thread didn't complete")

    def test_interrupt_signal(self):
        if sys.platform.startswith('java'):
            # We can't figure out how to raise an exception on a thread that's
            # blocked on a socket, whether that's the main thread or a worker,
            # without simply killing the whole thread in Jython. This suggests
            # PYTHON-294 can't actually occur in Jython.
            raise SkipTest("Can't test interrupts in Jython")

        # Test fix for PYTHON-294 -- make sure MongoClient closes its
        # socket if it gets an interrupt while waiting to recv() from it.
        db = self.client.pymongo_test

        # A $where clause which takes 1.5 sec to execute
        where = delay(1.5)

        # Need exactly 1 document so find() will execute its $where clause once
        db.drop_collection('foo')
        db.foo.insert({'_id': 1})

        def interrupter():
            # Raises KeyboardInterrupt in the main thread
            time.sleep(0.25)
            thread.interrupt_main()

        thread.start_new_thread(interrupter, ())

        raised = False
        try:
            # Will be interrupted by a KeyboardInterrupt.
            next(db.foo.find({'$where': where}))
        except KeyboardInterrupt:
            raised = True

        # Can't use self.assertRaises() because it doesn't catch system
        # exceptions
        self.assertTrue(raised, "Didn't raise expected KeyboardInterrupt")

        # Raises AssertionError due to PYTHON-294 -- Mongo's response to the
        # previous find() is still waiting to be read on the socket, so the
        # request id's don't match.
        self.assertEqual(
            {'_id': 1},
            next(db.foo.find())
        )

    def test_operation_failure_without_request(self):
        # Ensure MongoClient doesn't close socket after it gets an error
        # response to getLastError. PYTHON-395.
        pool = get_pool(self.client)
        socket_count = len(pool.sockets)
        self.assertGreaterEqual(socket_count, 1)
        old_sock_info = next(iter(pool.sockets))
        self.client.pymongo_test.test.drop()
        self.client.pymongo_test.test.insert({'_id': 'foo'})
        self.assertRaises(
            OperationFailure,
            self.client.pymongo_test.test.insert, {'_id': 'foo'})

        self.assertEqual(socket_count, len(pool.sockets))
        new_sock_info = next(iter(pool.sockets))
        self.assertEqual(old_sock_info, new_sock_info)

    def test_operation_failure_with_request(self):
        # Ensure MongoClient doesn't close socket after it gets an error
        # response to getLastError. PYTHON-395.
        c = rs_or_single_client()
        c.start_request()
        pool = get_pool(c)

        # Pool reserves a socket for this thread.
        c.pymongo_test.test.find_one()
        self.assertTrue(isinstance(pool._get_request_state(), SocketInfo))

        old_sock_info = pool._get_request_state()
        c.pymongo_test.test.drop()
        c.pymongo_test.test.insert({'_id': 'foo'})
        self.assertRaises(
            OperationFailure,
            c.pymongo_test.test.insert, {'_id': 'foo'})

        # OperationFailure doesn't affect the request socket
        self.assertEqual(old_sock_info, pool._get_request_state())

    def test_alive(self):
        self.assertTrue(self.client.alive())

        client = MongoClient('doesnt exist', connect=False)
        self.assertFalse(client.alive())

    def test_kill_cursors(self):
        if (client_context.is_mongos
                and not client_context.version.at_least(2, 4, 7)):
            # Old mongos sends incorrectly formatted error response when
            # cursor isn't found, see SERVER-9738.
            raise SkipTest("Can't test kill_cursors against old mongos")

        self.collection = self.client.pymongo_test.test
        self.collection.remove()
        
        # Ensure two batches.
        self.collection.insert({'_id': i} for i in range(200))

        cursor = self.collection.find()
        next(cursor)
        self.client.kill_cursors([cursor.cursor_id])
        
        with self.assertRaises(CursorNotFound):
            list(cursor)

    def test_lazy_connect_w0(self):
        client = rs_or_single_client(connect=False)
        client.pymongo_test.test.insert({}, w=0)

        client = rs_or_single_client(connect=False)
        client.pymongo_test.test.update({}, {'$set': {'x': 1}}, w=0)

        client = rs_or_single_client(connect=False)
        client.pymongo_test.test.remove(w=0)

    @client_context.require_no_mongos
    def test_exhaust_network_error(self):
        # When doing an exhaust query, the socket stays checked out on success
        # but must be checked in on error to avoid semaphore leaks.
        client = rs_or_single_client(max_pool_size=1)
        collection = client.pymongo_test.test
        pool = get_pool(client)
        pool._check_interval_seconds = None  # Never check.

        # Ensure a socket.
        connected(client)

        # Cause a network error.
        sock_info = one(pool.sockets)
        sock_info.sock.close()
        cursor = collection.find(exhaust=True)
        with self.assertRaises(ConnectionFailure):
            next(cursor)

        self.assertTrue(sock_info.closed)

        # The semaphore was decremented despite the error.
        self.assertTrue(pool._socket_semaphore.acquire(blocking=False))

    @client_context.require_auth
    def test_auth_network_error(self):
        # Make sure there's no semaphore leak if we get a network error
        # when authenticating a new socket with cached credentials.

        # Get a client with one socket so we detect if it's leaked.
        c = connected(rs_or_single_client(max_pool_size=1,
                                          waitQueueTimeoutMS=1))

        # Simulate an authenticate() call on a different socket.
        credentials = auth._build_credentials_tuple(
            'DEFAULT', 'admin', db_user, db_pwd, {})

        c._cache_credentials('test', credentials, connect=False)

        # Cause a network error on the actual socket.
        pool = get_pool(c)
        socket_info = one(pool.sockets)
        socket_info.sock.close()

        # SocketInfo.check_auth logs in with the new credential, but gets a
        # socket.error. Should be reraised as AutoReconnect.
        self.assertRaises(AutoReconnect, c.test.collection.find_one)

        # No semaphore leak, the pool is allowed to make a new socket.
        c.test.collection.find_one()

    @client_context.require_no_replica_set
    def test_connect_to_standalone_using_replica_set_name(self):
        with client_knobs(server_wait_time=0.1):
            client = MongoClient(pair, replicaSet='anything')

            with self.assertRaises(AutoReconnect):
                client.test.test.find_one()

    @client_context.require_replica_set
    def test_stale_getmore(self):
        # A cursor is created, but its member goes down and is removed from
        # the topology before the getMore message is sent. Test that
        # MongoClient._send_message_with_response handles the error.
        with client_knobs(server_wait_time=0.01):
            with self.assertRaises(AutoReconnect):
                self.client._send_message_with_response(
                    message=message.get_more('collection', 101, 1234),
                    address=('not-a-member', 27017))

    @client_context.require_replica_set
    def test_stale_killcursors(self):
        # A cursor is created, but its member goes down and is removed from
        # the topology before the killCursors message is sent. Test that
        # MongoClient._send_message handles the error.
        with client_knobs(server_wait_time=0.01):
            with self.assertRaises(AutoReconnect):
                self.client._send_message(
                    message=message.kill_cursors([1234]),
                    check_primary=False,
                    address=('not-a-member', 27017))


class TestExhaustCursor(IntegrationTest):
    """Test that clients properly handle errors from exhaust cursors."""

    def setUp(self):
        super(TestExhaustCursor, self).setUp()
        if client_context.is_mongos:
            raise SkipTest("mongos doesn't support exhaust, SERVER-2627")

    # mongod < 2.2.0 closes exhaust socket on error, so it behaves like
    # test_exhaust_query_network_error. Here we test that on query error
    # the client correctly keeps the socket *open* and checks it in.
    @client_context.require_version_min(2, 2, 0)
    def test_exhaust_query_server_error(self):
        # When doing an exhaust query, the socket stays checked out on success
        # but must be checked in on error to avoid semaphore leaks.
        client = connected(rs_or_single_client(max_pool_size=1))

        collection = client.pymongo_test.test
        pool = get_pool(client)
        sock_info = one(pool.sockets)

        # This will cause OperationFailure in all mongo versions since
        # the value for $orderby must be a document.
        cursor = collection.find(
            SON([('$query', {}), ('$orderby', True)]), exhaust=True)

        self.assertRaises(OperationFailure, cursor.next)
        self.assertFalse(sock_info.closed)

        # The socket was checked in and the semaphore was decremented.
        self.assertIn(sock_info, pool.sockets)
        self.assertTrue(pool._socket_semaphore.acquire(blocking=False))

    def test_exhaust_getmore_server_error(self):
        # When doing a getmore on an exhaust cursor, the socket stays checked
        # out on success but it's checked in on error to avoid semaphore leaks.
        client = rs_or_single_client(max_pool_size=1)
        collection = client.pymongo_test.test
        collection.remove()

        # Enough data to ensure it streams down for a few milliseconds.
        long_str = 'a' * (256 * 1024)
        collection.insert([{'a': long_str} for _ in range(200)])

        pool = get_pool(client)
        pool._check_interval_seconds = None  # Never check.
        sock_info = one(pool.sockets)

        cursor = collection.find(exhaust=True)

        # Initial query succeeds.
        cursor.next()

        # Cause a server error on getmore.
        client_context.client.pymongo_test.test.drop()
        self.assertRaises(OperationFailure, list, cursor)

        # The socket is still valid.
        self.assertIn(sock_info, pool.sockets)
        self.assertEqual(0, collection.count())

    def test_exhaust_query_network_error(self):
        # When doing an exhaust query, the socket stays checked out on success
        # but must be checked in on error to avoid semaphore leaks.
        client = connected(rs_or_single_client(max_pool_size=1))
        collection = client.pymongo_test.test
        pool = get_pool(client)
        pool._check_interval_seconds = None  # Never check.

        # Cause a network error.
        sock_info = one(pool.sockets)
        sock_info.sock.close()

        cursor = collection.find(exhaust=True)
        self.assertRaises(ConnectionFailure, cursor.next)
        self.assertTrue(sock_info.closed)

        # The socket was closed and the semaphore was decremented.
        self.assertNotIn(sock_info, pool.sockets)
        self.assertTrue(pool._socket_semaphore.acquire(blocking=False))

    def test_exhaust_getmore_network_error(self):
        # When doing a getmore on an exhaust cursor, the socket stays checked
        # out on success but it's checked in on error to avoid semaphore leaks.
        client = rs_or_single_client(max_pool_size=1)
        collection = client.pymongo_test.test
        collection.remove()
        collection.insert([{} for _ in range(200)])  # More than one batch.
        pool = get_pool(client)
        pool._check_interval_seconds = None  # Never check.

        cursor = collection.find(exhaust=True)

        # Initial query succeeds.
        cursor.next()

        # Cause a network error.
        sock_info = cursor._Cursor__exhaust_mgr.sock
        sock_info.sock.close()

        # A getmore fails.
        self.assertRaises(ConnectionFailure, list, cursor)
        self.assertTrue(sock_info.closed)

        # The socket was closed and the semaphore was decremented.
        self.assertNotIn(sock_info, pool.sockets)
        self.assertTrue(pool._socket_semaphore.acquire(blocking=False))


class TestClientProperties(MockClientTest):

    def test_wire_version_mongos_ha(self):
        # TODO: Reimplement Mongos HA with PyMongo 3's MongoClient.
        raise SkipTest('Mongos HA must be reimplemented in PyMongo 3')

        c = MockClient(
            standalones=[],
            members=[],
            mongoses=['a:1', 'b:2', 'c:3'],
            host='a:1,b:2,c:3',
            connect=False)

        c.set_wire_version_range('a:1', 2, 5)
        c.set_wire_version_range('b:2', 2, 2)
        c.set_wire_version_range('c:3', 1, 1)
        c.db.command('ismaster')  # Connect.

        # Which member did we use?
        used_host = '%s:%s' % (c.host, c.port)
        expected_min, expected_max = c.mock_wire_versions[used_host]
        self.assertEqual(expected_min, c.min_wire_version)
        self.assertEqual(expected_max, c.max_wire_version)

        c.set_wire_version_range('a:1', 0, 0)
        c.set_wire_version_range('b:2', 0, 0)
        c.set_wire_version_range('c:3', 0, 0)
        c.disconnect()
        c.db.command('ismaster')
        used_host = '%s:%s' % (c.host, c.port)
        expected_min, expected_max = c.mock_wire_versions[used_host]
        self.assertEqual(expected_min, c.min_wire_version)
        self.assertEqual(expected_max, c.max_wire_version)


class TestClientLazyConnect(IntegrationTest):
    """Test concurrent operations on a lazily-connecting MongoClient."""

    def _get_client(self):
        return rs_or_single_client(connect=False)

    def test_insert(self):
        def reset(collection):
            collection.drop()

        def insert(collection, _):
            collection.insert({})

        def test(collection):
            self.assertEqual(NTHREADS, collection.count())

        lazy_client_trial(reset, insert, test, self._get_client)

    def test_save(self):
        def reset(collection):
            collection.drop()

        def save(collection, _):
            collection.save({})

        def test(collection):
            self.assertEqual(NTHREADS, collection.count())

        lazy_client_trial(reset, save, test, self._get_client)

    def test_update(self):
        def reset(collection):
            collection.drop()
            collection.insert([{'i': 0}])

        # Update doc 10 times.
        def update(collection, i):
            collection.update({}, {'$inc': {'i': 1}})

        def test(collection):
            self.assertEqual(NTHREADS, collection.find_one()['i'])

        lazy_client_trial(reset, update, test, self._get_client)

    def test_remove(self):
        def reset(collection):
            collection.drop()
            collection.insert([{'i': i} for i in range(NTHREADS)])

        def remove(collection, i):
            collection.remove({'i': i})

        def test(collection):
            self.assertEqual(0, collection.count())

        lazy_client_trial(reset, remove, test, self._get_client)

    def test_find_one(self):
        results = []

        def reset(collection):
            collection.drop()
            collection.insert({})
            results[:] = []

        def find_one(collection, _):
            results.append(collection.find_one())

        def test(collection):
            self.assertEqual(NTHREADS, len(results))

        lazy_client_trial(reset, find_one, test, self._get_client)

    def test_max_bson_size(self):
        # Client should have sane defaults before connecting, and should update
        # its configuration once connected.
        c = self._get_client()
        self.assertEqual(16 * (1024 ** 2), c.max_bson_size)
        self.assertEqual(2 * c.max_bson_size, c.max_message_size)

        # Make the client connect, so that it sets its max_bson_size and
        # max_message_size attributes.
        ismaster = c.db.command('ismaster')
        self.assertEqual(ismaster['maxBsonObjectSize'], c.max_bson_size)
        if 'maxMessageSizeBytes' in ismaster:
            self.assertEqual(
                ismaster['maxMessageSizeBytes'],
                c.max_message_size)


class TestClientLazyConnectBadSeeds(IntegrationTest):
    def _get_client(self):
        # Assume there are no open mongods listening on a.com, b.com, ....
        bad_seeds = ['%s.com' % chr(ord('a') + i) for i in range(10)]
        return MongoClient(
            bad_seeds,
            replicaSet=client_context.replica_set_name,
            connect=False)

    def test_connect(self):
        def reset(dummy):
            pass

        def connect(collection, dummy):
            self.assertRaises(AutoReconnect, collection.find_one)

        def test(collection):
            client = collection.database.connection
            self.assertEqual(0, len(client.nodes))

        with client_knobs(server_wait_time=0.01):
            lazy_client_trial(reset, connect, test, self._get_client)


class TestMongoClientFailover(MockClientTest):

    def test_discover_primary(self):
        # Disable background refresh.
        with client_knobs(heartbeat_frequency=999999):
            c = MockClient(
                standalones=[],
                members=['a:1', 'b:2', 'c:3'],
                mongoses=[],
                host='b:2',  # Pass a secondary.
                replicaSet='rs')

            wait_until(lambda: len(c.nodes) == 3, 'connect')
            self.assertEqual('a', c.host)
            self.assertEqual(1, c.port)

            # Fail over.
            c.kill_host('a:1')
            c.mock_primary = 'b:2'

            c.disconnect()
            self.assertEqual(0, len(c.nodes))

            c._get_topology().select_servers(writable_server_selector)
            self.assertEqual('b', c.host)
            self.assertEqual(2, c.port)

            # a:1 not longer in nodes.
            self.assertLess(len(c.nodes), 3)
            wait_until(lambda: len(c.nodes) == 2, 'discover node "c"')

    def test_reconnect(self):
        # Verify the node list isn't forgotten during a network failure.
        c = MockClient(
            standalones=[],
            members=['a:1', 'b:2', 'c:3'],
            mongoses=[],
            host='b:2',  # Pass a secondary.
            replicaSet='rs')

        wait_until(lambda: len(c.nodes) == 3, 'connect')

        # Total failure.
        c.kill_host('a:1')
        c.kill_host('b:2')
        c.kill_host('c:3')

        # MongoClient discovers it's alone.
        self.assertRaises(AutoReconnect, c.db.collection.find_one)

        # But it can reconnect.
        c.revive_host('a:1')
        c._get_topology().select_servers(writable_server_selector)
        self.assertEqual('a', c.host)
        self.assertEqual(1, c.port)

    def test_network_error_on_operation(self):
        # Verify only the disconnected server is reset by a network failure.

        # Disable background refresh.
        with client_knobs(heartbeat_frequency=999999):
            c = MockClient(
                standalones=[],
                members=['a:1', 'b:2'],
                mongoses=[],
                host='a:1',
                replicaSet='rs',
                connect=False)

            # Set host-specific information so we can test whether it is reset.
            c.set_wire_version_range('a:1', 0, 1)
            c.set_wire_version_range('b:2', 0, 2)

            connected(c)
            wait_until(lambda: len(c.nodes) == 2, 'connect')

            sd = c._get_topology().get_server_by_address(('a', 1)).description
            self.assertEqual(SERVER_TYPE.RSPrimary, sd.server_type)
            self.assertEqual(0, sd.min_wire_version)
            self.assertEqual(1, sd.max_wire_version)

            c.kill_host('a:1')

            # MongoClient is disconnected from the primary.
            self.assertRaises(AutoReconnect, c.db.collection.find_one)

            # The primary's description is reset.
            sd_a = c._get_topology().get_server_by_address(('a', 1)).description
            self.assertEqual(SERVER_TYPE.Unknown, sd_a.server_type)
            self.assertEqual(0, sd_a.min_wire_version)
            self.assertEqual(0, sd_a.max_wire_version)

            # ...but not the secondary's.
            sd_b = c._get_topology().get_server_by_address(('b', 2)).description
            self.assertEqual(SERVER_TYPE.RSSecondary, sd_b.server_type)
            self.assertEqual(0, sd_b.min_wire_version)
            self.assertEqual(2, sd_b.max_wire_version)


if __name__ == "__main__":
    unittest.main()
