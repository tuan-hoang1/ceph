import errno
import json
import re
import os
import threading
import functools
import uuid
from subprocess import check_output, CalledProcessError

import six

from mgr_module import CLICommand, HandleCommandResult
from mgr_module import MgrModule, PersistentStoreDict

import orchestrator




class TestCompletionMixin(object):
    all_completions = []  # Hacky global

    def __init__(self, cb, message, *args, **kwargs):
        super(TestCompletionMixin, self).__init__(*args, **kwargs)
        self.cb = cb
        self._result = None
        self._complete = False

        self.message = message
        self.id = str(uuid.uuid4())

        TestCompletionMixin.all_completions.append(self)

    @property
    def result(self):
        return self._result

    @property
    def is_complete(self):
        return self._complete

    def execute(self):
        self._result = self.cb()
        self.executed = True
        self._complete = True

    def __str__(self):
        return "{}(result={} message={}, exception={})".format(self.__class__.__name__, self.result,
                                                               self.message, self.exception)


class TestReadCompletion(TestCompletionMixin, orchestrator.ReadCompletion):
    def __init__(self, cb):
        super(TestReadCompletion, self).__init__(cb, "<read op>")


class TestWriteCompletion(TestCompletionMixin, orchestrator.WriteCompletion):
    def __init__(self, cb, message):
        super(TestWriteCompletion, self).__init__(cb, message)

    @property
    def is_persistent(self):
        return (not self.is_errored) and self.executed

    @property
    def is_effective(self):
        return self._complete


def deferred_write(message):
    def wrapper(f):
        @functools.wraps(f)
        def inner(*args, **kwargs):
            return TestWriteCompletion(lambda: f(*args, **kwargs),
                                       '{}, args={}, kwargs={}'.format(message, args, kwargs))
        return inner
    return wrapper


def deferred_read(f):
    """
    Decorator to make TestOrchestrator methods return
    a completion object that executes themselves.
    """

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return TestReadCompletion(lambda: f(*args, **kwargs))

    return wrapper


class TestOrchestrator(MgrModule, orchestrator.Orchestrator):
    """
    This is an orchestrator implementation used for internal testing. It's meant for
    development environments and integration testing.

    It does not actually do anything.

    The implementation is similar to the Rook orchestrator, but simpler.
    """

    def wait(self, completions):
        self.log.info("wait: completions={0}".format(completions))

        # Our `wait` implementation is very simple because everything's
        # just an API call.
        for c in completions:
            if not isinstance(c, TestReadCompletion) and \
                    not isinstance(c, TestWriteCompletion):
                raise TypeError(
                    "wait() requires list of completions, not {0}".format(
                        c.__class__
                    ))

            if c.is_complete:
                continue

            try:
                c.execute()
            except Exception as e:
                self.log.exception("Completion {0} threw an exception:".format(
                    c.message
                ))
                c.exception = e
                c._complete = True

        return all(c.is_complete for c in completions)

    @CLICommand('test_orchestrator load_data', '', 'load dummy data into test orchestrator', 'w')
    def _load_data(self, inbuf):
        try:
            data = json.loads(inbuf)
            self._init_data(data)
            return HandleCommandResult()
        except json.decoder.JSONDecodeError as e:
            msg = 'Invalid JSON file: {}'.format(e)
            return HandleCommandResult(retval=-errno.EINVAL, stderr=msg)
        except orchestrator.OrchestratorValidationError as e:
            return HandleCommandResult(retval=-errno.EINVAL, stderr=str(e))

    def available(self):
        return True, ""

    def __init__(self, *args, **kwargs):
        super(TestOrchestrator, self).__init__(*args, **kwargs)

        self._initialized = threading.Event()
        self._shutdown = threading.Event()
        self._init_data({})

    def shutdown(self):
        self._shutdown.set()

    def serve(self):

        self._initialized.set()

        while not self._shutdown.is_set():
            # XXX hack (or is it?) to kick all completions periodically,
            # in case we had a caller that wait()'ed on them long enough
            # to get persistence but not long enough to get completion

            self.wait(TestCompletionMixin.all_completions)
            TestCompletionMixin.all_completions = [c for c in TestCompletionMixin.all_completions if
                                                   not c.is_complete]

            self._shutdown.wait(5)

    def _init_data(self, data=None):
        self._inventory = [orchestrator.InventoryNode.from_json(inventory_node)
                           for inventory_node in data.get('inventory', [])]
        self._services = [orchestrator.ServiceDescription.from_json(service)
                          for service in data.get('services', [])]

    @deferred_read
    def get_inventory(self, node_filter=None, refresh=False):
        """
        There is no guarantee which devices are returned by get_inventory.
        """
        if node_filter and node_filter.nodes is not None:
            assert isinstance(node_filter.nodes, list)

        if self._inventory:
            if node_filter:
                return list(filter(lambda node: node.name in node_filter.nodes,
                                   self._inventory))
            return self._inventory

        try:
            c_v_out = check_output(['ceph-volume', 'inventory', '--format', 'json'])
        except OSError:
            cmd = """
            . {tmpdir}/ceph-volume-virtualenv/bin/activate
            ceph-volume inventory --format json
            """
            try:
                c_v_out = check_output(cmd.format(tmpdir=os.environ.get('TMPDIR', '/tmp')), shell=True)
            except (OSError, CalledProcessError):
                c_v_out = check_output(cmd.format(tmpdir='.'),shell=True)

        for out in c_v_out.splitlines():
            self.log.error(out)
            devs = []
            for device in json.loads(out):
                dev = orchestrator.InventoryDevice.from_ceph_volume_inventory(device)
                devs.append(dev)
            return [orchestrator.InventoryNode('localhost', devs)]
        self.log.error('c-v failed: ' + str(c_v_out))
        raise Exception('c-v failed')

    @deferred_read
    def describe_service(self, service_type=None, service_id=None, node_name=None, refresh=False):
        """
        There is no guarantee which daemons are returned by describe_service, except that
        it returns the mgr we're running in.
        """
        if service_type:
            support_services = ("mds", "osd", "mon", "rgw", "mgr", "iscsi")
            assert service_type in support_services, service_type + " unsupported"

        if self._services:
            if node_name:
                return list(filter(lambda svc: svc.nodename == node_name, self._services))
            return self._services

        out = map(str, check_output(['ps', 'aux']).splitlines())
        types = [service_type] if service_type else ("mds", "osd", "mon", "rgw", "mgr")
        processes = [p for p in out if any([('ceph-' + t in p) for t in types])]

        result = []
        for p in processes:
            sd = orchestrator.ServiceDescription()
            sd.nodename = 'localhost'
            sd.service_instance = re.search('ceph-[^ ]+', p).group()
            result.append(sd)

        return result


    @deferred_write("create_osds")
    def create_osds(self, drive_group, all_hosts):
        drive_group.validate(all_hosts)

    @deferred_write("remove_osds")
    def remove_osds(self, osd_ids):
        assert isinstance(osd_ids, list)

    @deferred_write("service_action")
    def service_action(self, action, service_type, service_name=None, service_id=None):
        pass

    @deferred_write("Adding NFS service")
    def add_nfs(self, spec):
        assert isinstance(spec.pool, str)

    @deferred_write("remove_nfs")
    def remove_nfs(self, name):
        pass

    @deferred_write("update_nfs")
    def update_nfs(self, spec):
        pass

    @deferred_write("add_mds")
    def add_mds(self, spec):
        pass

    @deferred_write("remove_mds")
    def remove_mds(self, name):
        pass

    @deferred_write("add_rgw")
    def add_rgw(self, spec):
        pass

    @deferred_write("remove_rgw")
    def remove_rgw(self, zone):
        pass


    @deferred_read
    def get_hosts(self):
        if self._inventory:
            return self._inventory
        return [orchestrator.InventoryNode('localhost', [])]

    @deferred_write("add_host")
    def add_host(self, host):
        if host == 'raise_no_support':
            raise orchestrator.OrchestratorValidationError("MON count must be either 1, 3 or 5")
        if host == 'raise_bug':
            raise ZeroDivisionError()
        if host == 'raise_not_implemented':
            raise NotImplementedError()
        if host == 'raise_no_orchestrator':
            raise orchestrator.NoOrchestrator()
        if host == 'raise_import_error':
            raise ImportError("test_orchestrator not enabled")
        assert isinstance(host, six.string_types)

    @deferred_write("remove_host")
    def remove_host(self, host):
        assert isinstance(host, six.string_types)

    @deferred_write("update_mgrs")
    def update_mgrs(self, num, hosts):
        assert not hosts or len(hosts) == num
        assert all([isinstance(h, str) for h in hosts])

    @deferred_write("update_mons")
    def update_mons(self, num, hosts):
        assert not hosts or len(hosts) == num
        assert all([isinstance(h[0], str) for h in hosts])
        assert all([isinstance(h[1], str) or h[1] is None for h in hosts])