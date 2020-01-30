# oci-utils
#
# Copyright (c) 2017, 2019 Oracle and/or its affiliates. All rights reserved.
# Licensed under the Universal Permissive License v 1.0 as shown
# at http://oss.oracle.com/licenses/upl.

"""
Oracle Cloud Infrastructure utilities - daemon that polls for iscsi
and network configuration changes.
"""

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import daemon
import daemon.pidfile
import sdnotify

import oci_utils
import oci_utils.iscsiadm
import oci_utils.metadata
import oci_utils.oci_api
from oci_utils import _configuration as OCIUtilsConfiguration
from oci_utils import _MAX_VOLUMES_LIMIT
from oci_utils import vnicutils
from oci_utils.cache import get_timestamp, load_cache, write_cache

__ocid_logger = logging.getLogger('oci-utils.ocid')


def parse_args():
    """
    Parse the command line arguments and return an object representing the
    command line (as returned by argparse's parse_args())

    Returns
    -------
    namespace
        The commnad line namespace.
    """
    parser = argparse.ArgumentParser(description='oci-utils daemon')
    parser.add_argument('--refresh', nargs='?', action='store',
                        metavar='FUNC', default=False,
                        help='refresh cached data for the given function, '
                        'or all functions if FUNC is not specified, and exit. '
                        'Possible values for FUNC: vnic, iscsi, '
                        'public_ip')
    parser.add_argument('--no-daemon', action='store_true',
                        help='run ocid in the foreground, useful for debugging')
    parser.add_argument('--test', action='store_true', help=argparse.SUPPRESS)

    args = parser.parse_args()
    # when no argument is given set to True
    if args.refresh is None:
        args.refresh = True
    elif args.refresh:
        # not False and not None
        if args.refresh not in ['vnic', 'iscsi', 'public_ip']:
            sys.stderr.write('Invalid choice for --refresh\n')
            sys.exit(1)

    return args


class OcidThread(threading.Thread):
    """
    The ocid thread class.

    Attributes
    ----------
    thread_name : str
        The thread name.
    ocidfunc : func
        The function to be executed.
    context : dict
        The function context.
    sleeptime : int
        Interval to wait after each iteration.
    repeat : int
        The number of iterations.
    active : bool
        Indicator if thread is running.
    first_iter_done: bool
        Indicator if first iteration is finished.
    threadLock : lock
        The thread lock.
    """

    def __init__(self, name, ocidfunc, context, sleeptime, repeat=True):
        """
        Creates a new ocid thread with the given name. The thread will
        execute the given ocidfunc function with the given context as the
        argument in an infinite loop. Sleep for sleeptime seconds after
        each iteration. ocidfunc must return a new context, which is passed
        to ocidfunc in the next iteration.

        Parameters
        ----------
        name : str
            The tread name.
        ocidfunc : func
            The function to execute.
        context : dict
            The thread context
        sleeptime : int
            The time to wait after each iteration, in sec.
        repeat : int
            The number of iteration.
        """
        threading.Thread.__init__(self)
        self.thread_name = name
        self.ocidfunc = ocidfunc
        self.context = context
        if sleeptime < 1:
            self.sleeptime = 1
        else:
            try:
                self.sleeptime = int(sleeptime)
            except Exception:
                self.sleeptime = 60
        self.repeat = repeat
        self.active = False
        self.first_iter_done = False
        self.threadLock = threading.Lock()
        self.thr_logger = logging.getLogger('oci-utils.ocid.%s' % self.name)

    def __str__(self):
        return self.thread_name

    def getName(self):
        """
        Collect the thread name.

        Returns
        -------
        str
            The thread name.
        """
        return self.thread_name

    def stop(self):
        """
        Tell the thread to stop.

        Returns
        -------
            No return value.
        """
        # de-activate
        self.active = False

    def get_context(self):
        """
        Collect the context of the thread.

        Returns
        -------
        dict
            The context.
        """
        # make sure it's not running
        self.threadLock.acquire()
        # save context
        ctx = self.context
        self.threadLock.release()
        return ctx

    def is_first_iter_done(self):
        """
        Verify the end of the first iteration.

        Returns
        -------
        bool
            True or Ralse.
        """
        return self.first_iter_done

    def run(self):
        """
        Execute.

        Returns
        -------
            No return value.
        """
        self.active = True
        self.thr_logger.info("Starting ocid thread '%s'" % self.thread_name)
        while True:
            self.thr_logger.debug("Running thread func for "
                                  "thread %s" % self.thread_name)
            self.threadLock.acquire()
            if not self.active:
                # shutting down.
                self.threadLock.release()
                sys.exit(0)
            try:
                self.context = self.ocidfunc(self.context, self.thr_logger)
            except Exception as e:
                self.thr_logger.error("Error running ocid "
                                      "thread '%s': %s" % (self.thread_name, e))
                self.thr_logger.exception(e)
            self.threadLock.release()
            # ocidfunc completed at least once
            self.first_iter_done = True
            if not self.repeat:
                # run the main function once only
                break
            time_slept = 0
            # sleep sleep_unit sec at a time so we can shut down cleanly in
            # no more than that time
            sleep_unit = 10
            while time_slept < self.sleeptime:
                if self.sleeptime - time_slept < sleep_unit:
                    time.sleep(self.sleeptime - time_slept)
                    time_slept = self.sleeptime
                else:
                    time.sleep(sleep_unit)
                    time_slept += sleep_unit
                if not self.active:
                    # shutting down
                    sys.exit(0)


def test_func(context):
    """
    OCID thread function for testing purposes.

    Parameters
    ----------
    context : dict
        The thread context.

    Returns
    -------
    dict
        The new context.
    """
    if 'fname' not in context:
        raise ValueError("fname must be defined in the context")
    if 'counter' not in context:
        raise ValueError("counter must be defined in the context")
    with open(context['fname'], "w+") as f:
        f.write("hello %d\n" % context['counter'])
    context['counter'] += 1
    return context


def public_ip_func(context, func_logger):
    """
    OCID thread function for refreshing the instance metadata.

    Parameters
    ----------
    context: dict
        THe thread context.
        # --GT-- not used, kept to avoid to break the function call.

    Returns
    -------
    dict
        Dictionary containiong the external IP address.
    """
    if oci_utils.oci_api.HAVE_OCI_SDK:
        try:
            sess = oci_utils.oci_api.OCISession()
            return {'publicIp': sess.this_instance().get_public_ip()}
        except OCISDKError, e:
            # enough for now
            # TODO : use thread subclass instead of func indirection
            pass
    # fallback
    external_ip = get_ip_info()[1]

    return {'publicIp': external_ip}


def iscsi_func(context, func_logger):
    """
    OCID thread function for discovering and attaching/detaching block
    volumes; context must include 'max_volumes' and 'auto_detach'.

    Parameters
    ----------
    context: dict
        The thread context.

    Returns
    -------
    dict
        The new context.
    """
    if 'oci_sess' not in context:
        oci_sess = None
        if oci_utils.oci_api.HAVE_OCI_SDK:
            try:
                oci_sess = oci_utils.oci_api.OCISession()
            except Exception:
                pass
        max_volumes = 8
        try:
            max_volumes = int(context['max_volumes'])
        except Exception:
            pass
        auto_detach = True
        try:
            auto_detach = context['auto_detach']
        except Exception:
            pass
        # the number of iterations to wait before detaching an offline volume
        detach_retry = 5
        try:
            detach_retry = int(context['detach_retry'])
        except Exception:
            pass
        if max_volumes > _MAX_VOLUMES_LIMIT:
            func_logger.warn("Your configured max_volumes(%s) is over the "
                             "limit(%s)\n" %
                             (max_volumes, _MAX_VOLUMES_LIMIT))
            max_volumes = _MAX_VOLUMES_LIMIT
        context = {'ignore_file_ts': 0,
                   'ignore_iqns': [],
                   'attach_failed': {},
                   'chap_pw_ts': 0,
                   'chap_pws': {},
                   'oci_sess': oci_sess,
                   'max_volumes': max_volumes,
                   'offline_vols': {},
                   'auto_detach': auto_detach,
                   'detach_retry': detach_retry, }

    # devices currently attached
    session_devs = oci_utils.iscsiadm.session()

    # Load the saved passwords
    chap_passwords = context['chap_pws']
    if context['chap_pw_ts'] == 0 or \
            get_timestamp(oci_utils.__chap_password_file) > context['chap_pw_ts']:
        # the password file has changed or was never loaded
        context['chap_pw_ts'], chap_passwords = \
            load_cache(oci_utils.__chap_password_file)
    if chap_passwords is None:
        chap_passwords = {}
    # save for the next iteration
    context['chap_pws'] = chap_passwords

    # volumes that are offline in this iteration
    new_offline_vols = {}

    all_iqns = {}

    # volumes connected to this instance
    inst_volumes = []
    if context['oci_sess'] is not None:
        # get a list of volumes attached to the instance
        volumes = context['oci_sess'].this_instance().all_volumes(refresh=True)
        for v in volumes:
            vol = {'iqn': v.get_iqn(),
                   'ipaddr': v.get_portal_ip(),
                   'user': v.get_user(),
                   'password': v.get_password()}
            inst_volumes.append(vol)
            if v.get_portal_ip() in all_iqns:
                all_iqns[v.get_portal_ip()].append(v.get_iqn())
            else:
                all_iqns[v.get_portal_ip()] = [v.get_iqn()]
    else:
        # fall back to scanning
        for r in range(context['max_volumes'] + 1):
            ipaddr = "169.254.2.%d" % (r + 1)
            iqns = oci_utils.iscsiadm.discovery(ipaddr)
            all_iqns[ipaddr] = iqns
            for iqn in iqns:
                vol = {'iqn': iqn,
                       'ipaddr': ipaddr,
                       'user': None,
                       'password': None}
                # look for a saved password
                if iqn in chap_passwords:
                    vol['user'] = chap_passwords[iqn][0]
                    vol['password'] = chap_passwords[iqn][1]
                inst_volumes.append(vol)

    # Load the list of volumes that were detached using oci-iscsi-config.
    # ocid shouldn't attach these automatically.
    ignore_iqns = context['ignore_iqns']
    if context['ignore_file_ts'] == 0 or \
            get_timestamp(oci_utils.__ignore_file) > context['ignore_file_ts']:
        # the list of detached volumes changed since last reading the file
        context['ignore_file_ts'], ignore_iqns = \
            load_cache(oci_utils.__ignore_file)
    if ignore_iqns is None:
        ignore_iqns = []
    # save for next iteration
    context['ignore_iqns'] = ignore_iqns

    # volumes that failed to attach in an earlier iteration
    attach_failed = context['attach_failed']

    # do we need to cache files?
    cache_changed = False
    ign_changed = False
    chap_changed = False

    # check if all discovered iscsi devices are configured and attached
    for vol in inst_volumes:
        if vol['iqn'] in ignore_iqns:
            # a device that was manually detached, so don't
            # re-attach it automatically
            continue
        if vol['iqn'] not in session_devs:
            if vol['iqn'] in attach_failed:
                # previous attempt to attach failed, ignore
                continue
            cache_changed = True
            # configure and attach the device
            __ocid_logger.info("Attaching iscsi device: %s:%s (%s)" %
                               (vol['ipaddr'], "3260", vol['iqn']))
            if vol['user'] is not None:
                attach_result = \
                    oci_utils.iscsiadm.attach(vol['ipaddr'], 3260,
                                              vol['iqn'], vol['user'],
                                              vol['password'],
                                              auto_startup=True)
                if vol['iqn'] not in chap_passwords:
                    chap_passwords[vol['iqn']] = (vol['user'], vol['password'])
                    chap_changed = True
            else:
                attach_result = \
                    oci_utils.iscsiadm.attach(vol['ipaddr'], 3260,
                                              vol['iqn'],
                                              auto_startup=True)
            if attach_result != 0:
                func_logger.info("Failed to attach device: %s" %
                                 oci_utils.iscsiadm.error_message_from_code(
                                     attach_result))
                attach_failed[vol['iqn']] = attach_result
                cache_changed = True

    # look for perviously failed volumes that are now in the session
    # (e.g. the user supplied the password using oci-iscsi-config)
    for iqn in attach_failed.keys():
        if iqn in session_devs:
            del attach_failed[iqn]
            cache_changed = True

    detach_retry = 5
    try:
        detach_retry = int(context['detach_retry'])
    except Exception:
        pass

    # look for disconnected devices in the current session
    # these devices were disconnected from the instance in the console,
    # we now have to detach them from at the OS level
    for iqn in session_devs:
        # ignore the boot device
        if iqn.endswith('boot:uefi'):
            continue
        if 'state' not in session_devs[iqn]:
            continue
        if session_devs[iqn]['state'] in ['blocked', 'transport-offline']:
            func_logger.debug("Checking iqn %s (state %s)\n" %
                              (iqn, session_devs[iqn]['state']))
            # is the iqn discoverable at the portal?
            if iqn not in inst_volumes:
                # Not found by iscsiadm discovery.
                # To allow time for the volume to recover, wait for detach_retry
                # iterations where the volume was offline before detaching it
                if iqn not in context['offline_vols']:
                    func_logger.info("iSCSI volume appears to be "
                                     "offline: %s" % iqn)
                    new_offline_vols[iqn] = 1
                    continue
                elif context['offline_vols'][iqn] < detach_retry:
                    new_offline_vols[iqn] = context['offline_vols'][iqn] + 1
                    func_logger.info("iSCSI volume still offline (%d): %s"
                                     % (new_offline_vols[iqn], iqn))
                    continue

                if not context['auto_detach']:
                    func_logger.info("Volume still offline, but iSCSI "
                                     "auto_detach disabled: %s" % iqn)
                    new_offline_vols[iqn] = detach_retry + 1
                    continue

                cache_changed = True
                ipaddr = session_devs[iqn]['persistent_portal_ip']
                func_logger.info("Detaching iSCSI device: %s:%s (%s)" %
                                 (ipaddr, "3260", iqn))
                oci_utils.iscsiadm.detach(ipaddr, 3260, iqn)
                # delete from list of previously offline volumes so it
                # doesn't get reported as 'now online'
                del context['offline_vols'][iqn]
                # device is gone, remove from "ignore" list
                if iqn in ignore_iqns:
                    ignore_iqns.remove(iqn)
                    ign_changed = True
                # remove from attach_failed list if present
                if iqn in attach_failed:
                    del attach_failed[iqn]
                    cache_changed = True

    # look for deviced that were previously offline but now back online
    # (just for printing a message that it's now online)
    for iqn in context['offline_vols']:
        if iqn not in new_offline_vols:
            func_logger.info("iSCSI volume now online: %s" % iqn)
    context['offline_vols'] = new_offline_vols

    # check if the devices that were previously manually detached are still
    # connected to the instance
    inst_iqns = [vol['iqn'] for vol in inst_volumes]
    for iqn in ignore_iqns:
        if iqn not in inst_iqns:
            func_logger.debug("Removing iqn %s from ignore list" % iqn)
            ignore_iqns.remove(iqn)
            ign_changed = True

    # rewrite changed cache files
    if ign_changed:
        context['ignore_file_ts'] = \
            write_cache(cache_content=ignore_iqns,
                        cache_fname=oci_utils.__ignore_file)
    if chap_changed:
        context['chap_pw_ts'] = \
            write_cache(cache_content=chap_passwords,
                        cache_fname=oci_utils.__chap_password_file,
                        mode=0o600)
    if cache_changed or not os.path.exists(oci_utils.iscsiadm.ISCSIADM_CACHE):
        write_cache(cache_content=[all_iqns, attach_failed],
                    cache_fname=oci_utils.iscsiadm.ISCSIADM_CACHE)
    else:
        try:
            os.utime(oci_utils.iscsiadm.ISCSIADM_CACHE, None)
        except Exception as e:
            func_logger.warn("Failed to update cache timestamp: %s" % e)

    return context


def get_metadata_vnics():
    """
    Collect the VNICs from the metadata.

    Returns
    -------
    list
        List of vnic's.
    """
    return oci_utils.metadata.InstanceMetadata(get_public_ip=False)['vnics']


def vnic_func(context, func_logger):
    """
    OCID thread function to track vnic configuration changes
     context: {'vnic_utils': None, 'vf_net':vf_net, 'logger':logger}
    Parameters
    ----------
    context: dict
        The thread context.
        # {'vnic_utils': None, 'vf_net':vf_net}
    func_logger: logger
     logger to use for debug and info
    Returns
    -------
    dict
        The new context.
    """

    func_logger.debug('Entering vnic_func, context == %s' % str(context))

    if context['vf_net']:
        # Don't do any network configuration if something
        # else is doing it
        func_logger.debug("Don't do any network configuration if something else is doing it")
        return context

    if context['vnic_utils'] is None:
        func_logger.debug("context['vnic_utils'] is None : first iteration")
        # first iteration
        oci_sess = None
        if oci_utils.oci_api.HAVE_OCI_SDK:
            try:
                oci_sess = oci_utils.oci_api.OCISession()
            except Exception:
                func_logger.debug("error getting session")
        context['oci_sess'] = oci_sess
        context['vnic_utils'] = vnicutils.VNICUtils()
        context['vnic_info_ts'], context['vnic_info'] = \
            context['vnic_utils'].get_vnic_info()
        context['vnics'] = get_metadata_vnics()
        (ret, out) = context['vnic_utils'].auto_config([], False, False)
        if ret != 0:
            func_logger.warning("Failed to configure network interfaces")
        if out is not None and out != "":
            func_logger.info("secondary VNIC script reports: %s" % out)
        func_logger.debug("Returning the context [%s]" % str(context))
        return context

    # If the vnic_info file changed then oci-network-config was invoked
    # and made changes.  We have to avoid undoing those changes.  Rebuild
    # the context instead.
    if vnicutils.VNICUtils.get_vnic_info_timestamp() > \
            context['vnic_info_ts']:
        func_logger.debug("vnic_info file changed, rebuilding context")
        context['vnic_info_ts'], context['vnic_info'] = \
            context['vnic_utils'].get_vnic_info()
        context['vnics'] = get_metadata_vnics()
        func_logger.debug("Returning the context [%s]" % str(context))
        return context

    vnics = get_metadata_vnics()

    func_logger.debug("VNICs from metadata [%s]" % str(vnics))

    update_needed = False

    if vnics != context['vnics']:
        # VNIC configuration changed
        func_logger.info("VNIC configuration changed.")
        update_needed = True
        context['vnics'] = vnics

    if context['oci_sess'] is not None:
        func_logger.debug("look for new or removed secondary private IP addresses")
        p_ips = context['oci_sess'].this_instance(). \
            all_private_ips(refresh=True)
        sec_priv_ip = \
            [[ip.get_address(), ip.get_vnic_ocid()] for ip in p_ips]
        for ip in sec_priv_ip:
            if ip not in context['vnic_info']['sec_priv_ip']:
                func_logger.info("New secondary private IP: %s" %
                                 ip[0])
                update_needed = True
        for ip in context['vnic_info']['sec_priv_ip']:
            if ip not in sec_priv_ip:
                func_logger.info("Secondary private IP "
                                 "disconnected: %s" % ip[0])
                update_needed = True
        if update_needed:
            # this one is used for comparing with the previous iteration
            context['vnic_info']['sec_priv_ip'] = sec_priv_ip
            context['vnic_utils'].set_private_ips(sec_priv_ip)

    func_logger.debug('update_needed == %s' % str(update_needed))

    if update_needed:
        func_logger.info("updating network interfaces")
        (ret, out) = context['vnic_utils'].auto_config([], False, False)
        if ret != 0:
            func_logger.warning("Failed to configure network interfaces")
        if out is not None and out != "":
            func_logger.info("secondary VNIC script reports: %s" % out)

    return context


def start_thread(name, repeat):
    """
    Start a specific thread.

    Parameters
    ----------
    name: str
        The name of the thread.
    repeat: bool
        Repeat the thread if set.

    Returns
    -------
    dict
        The thread object.
    """

    true_list = ['true', 'True', 'TRUE']

    if name == 'test':
        th = OcidThread(name=name,
                        ocidfunc=test_func,
                        context={'fname': '/tmp/ocid-test', 'counter': 1},
                        sleeptime=10,
                        repeat=repeat)
    elif name == 'public_ip':
        is_enabled = OCIUtilsConfiguration.get('public_ip', 'enabled')
        if is_enabled not in true_list:
            return None
        refresh_interval = \
            OCIUtilsConfiguration.get('public_ip', 'refresh_interval')
        th = OcidThread(name=name,
                        ocidfunc=public_ip_func,
                        context={},
                        sleeptime=int(refresh_interval),
                        repeat=repeat)
    elif name == 'iscsi':
        max_volumes = OCIUtilsConfiguration.get('iscsi', 'max_volumes')
        is_enabled = OCIUtilsConfiguration.get('iscsi', 'enabled')
        auto_detach = \
            OCIUtilsConfiguration.get('iscsi', 'auto_detach') in true_list
        if is_enabled not in true_list:
            return None
        # oci-growfs
        auto_resize = OCIUtilsConfiguration.get('iscsi', 'auto_resize')
        if auto_resize in true_list:
            try:
                output = subprocess.check_output(
                    ['/usr/libexec/oci-growfs', '-y'], stderr=subprocess.STDOUT)
            except Exception:
                pass

        scan_interval = OCIUtilsConfiguration.get('iscsi', 'scan_interval')
        th = OcidThread(name=name,
                        ocidfunc=iscsi_func,
                        context={'max_volumes': max_volumes,
                                 'auto_detach': auto_detach, },
                        sleeptime=int(scan_interval),
                        repeat=repeat)
    elif name == 'vnic':
        is_enabled = OCIUtilsConfiguration.get('vnic', 'enabled')
        if is_enabled not in true_list:
            return None
        scan_interval = OCIUtilsConfiguration.get('vnic', 'scan_interval')
        __ocid_logger.debug('scan interval for vnics: %s' % str(scan_interval))
        vf_net = OCIUtilsConfiguration.get('vnic', 'vf_net') in true_list
        th = OcidThread(name=name,
                        ocidfunc=vnic_func,
                        context={'vnic_utils': None,
                                 'vf_net': vf_net},
                        sleeptime=int(scan_interval),
                        repeat=repeat)
    else:
        __ocid_logger.error('Internal error: unknown thread: %s' % name)
        return None

    th.start()
    return th


def start_threads(args, repeat):
    """
    Start all threads according to the configuration or args.refresh.

    Parameters
    ----------
    args: namespace
        The parsed command line.
    repeat: bool
        Repeat the thread if set.

    Returns
    -------
    dict
        A dictionary with the thread name.
        # {thread_name: thread}
    """
    # set up threads
    threads = {}

    if args.test:
        # start the test thread
        __ocid_logger.debug('starting thread \'test\'')
        th = start_thread('test', repeat)
        if th:
            threads['test'] = th
    if not args.refresh or args.refresh is True:
        # is not a refresh request or is a request to refresh all
        # so start all threads
        __ocid_logger.debug('starting thread \'public_ip\'')
        th = start_thread('public_ip', repeat)
        if th:
            threads['public_ip'] = th
        __ocid_logger.debug('starting thread \'iscsi\'')
        th = start_thread('iscsi', repeat)
        if th:
            threads['iscsi'] = th
        __ocid_logger.debug('starting thread \'vnic\'')
        th = start_thread('vnic', repeat)
        if th:
            threads['vnic'] = th
    elif args.refresh:
        # start a specific thread only
        th = start_thread(args.refresh, repeat)
        if th:
            threads[args.refresh] = th
        return threads

    return threads


def monitor_threads(threads, arguments):
    """
    Monitor the threads.

    Parameters
    ----------
    threads: dict
        The threads to monitor.
    arguments: namespace
        The parsed command line.
        # --GT-- not used, kept to avoid to break the function call.

    Returns
    -------
    int
        0
    """
    try:
        # exit and let systemd restart the process to avoid issues with
        # potential memory leaks
        time.sleep(60 * 60 * 2)
    except Exception:
        # the sleep was interrupted
        pass

    for th in threads.keys():
        threads[th].stop()

    # give up to 30 seconds for threads to exit cleanly
    timeout = datetime.now() + timedelta(seconds=30)
    while timeout > datetime.now():
        thread_running = False
        for th in threads.keys():
            if threads[th].is_alive():
                thread_running = True
        if not thread_running:
            break
    return 0


def wait_for_threads(threads):
    """
    Wait for threads to finish.

    Parameters
    ----------
    threads: dict
        The threads to wait for.

    Returns
    -------
    int
        0
    """
    for th in threads.keys():
        threads[th].join()
        __ocid_logger.debug('Thread %s finished.' % th)
    return 0


def daemon_main(arguments):
    """
    Start and monitor the threads.

    Parameters
    ----------
    arguments: namespace
        The parsed command line.

    Returns
    -------
        No return value.
    """

    if arguments.refresh:
        # run the selected threads once
        threads = start_threads(arguments, repeat=False)
        result = wait_for_threads(threads)
        sys.exit(result)

    threads = start_threads(arguments, repeat=True)
    __ocid_logger.debug('threads started')
    # wait for every thread to complete the ocid func at least once
    first_iter_done = False
    while not first_iter_done:
        first_iter_done = True
        for th in threads.keys():
            if not threads[th].is_first_iter_done():
                # not finished yet
                first_iter_done = False
                __ocid_logger.debug('waiting for thread %s to finish the '
                                    'first iteration' % th)
                time.sleep(1)

    __ocid_logger.debug('all threads finished the first iteration')

    if arguments.no_daemon:
        os._exit(monitor_threads(threads, arguments))
    else:
        # Inform systemd that dependent services can now start
        notifier = sdnotify.SystemdNotifier()
        notifier.notify("READY=1")

    monitor_threads(threads, arguments)


def main():
    """
    Main program.

    Returns
    -------
        No return value.
    """
    try:
        if os.geteuid() != 0:
            sys.stderr.write("This program must be run as root.\n")
            return 1

        pidlock = daemon.pidfile.PIDLockFile('/var/run/ocid.pid')

        args = parse_args()

        if pidlock.is_locked():
            if not args.refresh:
                sys.stderr.write("ocid already running.\n")
                return 1
        __ocid_logger.debug('Starting daemon...')
        if args.no_daemon:
            daemon_main(args)
        else:
            daemon_context = daemon.DaemonContext(pidfile=pidlock, umask=0o033)
            with daemon_context:
                daemon_main(args)
        return 0

    except Exception as e:
        print e


if __name__ == "__main__":
    sys.exit(main())