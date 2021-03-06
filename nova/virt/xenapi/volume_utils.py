# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010 Citrix Systems, Inc.
# Copyright (c) 2013 Openstack, LLC.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Helper methods for operations related to the management of volumes,
and storage repositories
"""

import re
import string

from nova.openstack.common import cfg
from nova.openstack.common import log as logging


CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class StorageError(Exception):
    """To raise errors related to SR, VDI, PBD, and VBD commands."""

    def __init__(self, message=None):
        super(StorageError, self).__init__(message)


def _handle_sr_params(params):
    if 'id' in params:
        del params['id']

    sr_type = params.pop('sr_type', 'iscsi')
    sr_desc = params.pop('name_description', '')
    return sr_type, sr_desc


def create_sr(session, label, params):
    LOG.debug(_('Creating SR %(label)s') % locals())
    sr_type, sr_desc = _handle_sr_params(params)
    sr_ref = session.call_xenapi("SR.create",
                session.get_xenapi_host(),
                params,
                '0', label, sr_desc, sr_type, '', False, {})
    return sr_ref


def introduce_sr(session, sr_uuid, label, params):
    LOG.debug(_('Introducing SR %(label)s') % locals())

    sr_type, sr_desc = _handle_sr_params(params)

    sr_ref = session.call_xenapi('SR.introduce', sr_uuid, label, sr_desc,
            sr_type, '', False, params)

    LOG.debug(_('Creating PBD for SR'))
    pbd_ref = create_pbd(session, sr_ref, params)

    LOG.debug(_('Plugging SR'))
    session.call_xenapi("PBD.plug", pbd_ref)

    session.call_xenapi("SR.scan", sr_ref)
    return sr_ref


def forget_sr(session, sr_ref):
    """
    Forgets the storage repository without destroying the VDIs within
    """
    LOG.debug(_('Forgetting SR...'))
    unplug_pbds(session, sr_ref)
    session.call_xenapi("SR.forget", sr_ref)


def find_sr_by_uuid(session, sr_uuid):
    """
    Return the storage repository given a uuid.
    """
    for sr_ref, sr_rec in session.get_all_refs_and_recs('SR'):
        if sr_rec['uuid'] == sr_uuid:
            return sr_ref
    return None


def find_sr_from_vbd(session, vbd_ref):
    """Find the SR reference from the VBD reference."""
    try:
        vdi_ref = session.call_xenapi("VBD.get_VDI", vbd_ref)
        sr_ref = session.call_xenapi("VDI.get_SR", vdi_ref)
    except session.XenAPI.Failure, exc:
        LOG.exception(exc)
        raise StorageError(_('Unable to find SR from VBD %s') % vbd_ref)
    return sr_ref


def create_pbd(session, sr_ref, params):
    pbd_rec = {}
    pbd_rec['host'] = session.get_xenapi_host()
    pbd_rec['SR'] = sr_ref
    pbd_rec['device_config'] = params
    pbd_ref = session.call_xenapi("PBD.create", pbd_rec)
    return pbd_ref


def unplug_pbds(session, sr_ref):
    try:
        pbds = session.call_xenapi("SR.get_PBDs", sr_ref)
    except session.XenAPI.Failure, exc:
        LOG.warn(_('Ignoring exception %(exc)s when getting PBDs'
                   ' for %(sr_ref)s') % locals())
        return

    for pbd in pbds:
        try:
            session.call_xenapi("PBD.unplug", pbd)
        except session.XenAPI.Failure, exc:
            LOG.warn(_('Ignoring exception %(exc)s when unplugging'
                       ' PBD %(pbd)s') % locals())


def introduce_vdi(session, sr_ref, vdi_uuid=None, target_lun=None):
    """Introduce VDI in the host."""
    try:
        session.call_xenapi("SR.scan", sr_ref)
        if vdi_uuid:
            LOG.debug("vdi_uuid: %s" % vdi_uuid)
            vdi_ref = session.call_xenapi("VDI.get_by_uuid", vdi_uuid)
        elif target_lun:
            vdi_refs = session.call_xenapi("SR.get_VDIs", sr_ref)
            for curr_ref in vdi_refs:
                curr_rec = session.call_xenapi("VDI.get_record", curr_ref)
                if ('sm_config' in curr_rec and
                        'LUNid' in curr_rec['sm_config'] and
                        curr_rec['sm_config']['LUNid'] == str(target_lun)):
                    vdi_ref = curr_ref
                    break
        else:
            vdi_ref = (session.call_xenapi("SR.get_VDIs", sr_ref))[0]
    except session.XenAPI.Failure, exc:
        LOG.exception(exc)
        raise StorageError(_('Unable to introduce VDI on SR %s') % sr_ref)

    try:
        vdi_rec = session.call_xenapi("VDI.get_record", vdi_ref)
        LOG.debug(vdi_rec)
        LOG.debug(type(vdi_rec))
    except session.XenAPI.Failure, exc:
        LOG.exception(exc)
        raise StorageError(_('Unable to get record'
                             ' of VDI %s on') % vdi_ref)

    if vdi_rec['managed']:
        # We do not need to introduce the vdi
        return vdi_ref

    try:
        return session.call_xenapi("VDI.introduce",
                                    vdi_rec['uuid'],
                                    vdi_rec['name_label'],
                                    vdi_rec['name_description'],
                                    vdi_rec['SR'],
                                    vdi_rec['type'],
                                    vdi_rec['sharable'],
                                    vdi_rec['read_only'],
                                    vdi_rec['other_config'],
                                    vdi_rec['location'],
                                    vdi_rec['xenstore_data'],
                                    vdi_rec['sm_config'])
    except session.XenAPI.Failure, exc:
        LOG.exception(exc)
        raise StorageError(_('Unable to introduce VDI for SR %s')
                            % sr_ref)


def purge_sr(session, sr_ref):
    # Make sure no VBDs are referencing the SR VDIs
    vdi_refs = session.call_xenapi("SR.get_VDIs", sr_ref)
    for vdi_ref in vdi_refs:
        vbd_refs = session.call_xenapi("VDI.get_VBDs", vdi_ref)
        if vbd_refs:
            LOG.warn(_('Cannot purge SR with referenced VDIs'))
            return

    forget_sr(session, sr_ref)


def get_device_number(mountpoint):
    device_number = mountpoint_to_number(mountpoint)
    if device_number < 0:
        raise StorageError(_('Unable to obtain target information'
                ' %(mountpoint)s') % locals())
    return device_number


def parse_sr_info(connection_data, description=''):
    label = connection_data.pop('name_label',
                                'tempSR-%s' % connection_data.get('volume_id'))
    params = {}
    if 'sr_uuid' not in connection_data:
        params = parse_volume_info(connection_data)
        # This magic label sounds a lot like 'False Disc' in leet-speak
        uuid = "FA15E-D15C-" + str(params['id'])
    else:
        uuid = connection_data['sr_uuid']
        for k in connection_data.get('introduce_sr_keys', {}):
            params[k] = connection_data[k]
    params['name_description'] = connection_data.get('name_description',
                                                     description)

    return (uuid, label, params)


def parse_volume_info(connection_data):
    """
    Parse device_path and mountpoint as they can be used by XenAPI.
    In particular, the mountpoint (e.g. /dev/sdc) must be translated
    into a numeric literal.
    """
    volume_id = connection_data['volume_id']
    target_portal = connection_data['target_portal']
    target_host = _get_target_host(target_portal)
    target_port = _get_target_port(target_portal)
    target_iqn = connection_data['target_iqn']
    LOG.debug('(vol_id,number,host,port,iqn): (%s,%s,%s,%s)',
              volume_id, target_host, target_port, target_iqn)
    if (volume_id is None or
        target_host is None or
        target_iqn is None):
        raise StorageError(_('Unable to obtain target information'
                ' %(connection_data)s') % locals())
    volume_info = {}
    volume_info['id'] = volume_id
    volume_info['target'] = target_host
    volume_info['port'] = target_port
    volume_info['targetIQN'] = target_iqn
    if ('auth_method' in connection_data and
        connection_data['auth_method'] == 'CHAP'):
        volume_info['chapuser'] = connection_data['auth_username']
        volume_info['chappassword'] = connection_data['auth_password']

    return volume_info


def mountpoint_to_number(mountpoint):
    """Translate a mountpoint like /dev/sdc into a numeric."""
    if mountpoint.startswith('/dev/'):
        mountpoint = mountpoint[5:]
    if re.match('^[hs]d[a-p]$', mountpoint):
        return (ord(mountpoint[2:3]) - ord('a'))
    elif re.match('^x?vd[a-p]$', mountpoint):
        return (ord(mountpoint[-1]) - ord('a'))
    elif re.match('^[0-9]+$', mountpoint):
        return string.atoi(mountpoint, 10)
    else:
        LOG.warn(_('Mountpoint cannot be translated: %s'), mountpoint)
        return -1


def _get_volume_id(path_or_id):
    """Retrieve the volume id from device_path."""
    # If we have the ID and not a path, just return it.
    if isinstance(path_or_id, int):
        return path_or_id
    # n must contain at least the volume_id
    # :volume- is for remote volumes
    # -volume- is for local volumes
    # see compute/manager->setup_compute_volume
    volume_id = path_or_id[path_or_id.find(':volume-') + 1:]
    if volume_id == path_or_id:
        volume_id = path_or_id[path_or_id.find('-volume--') + 1:]
        volume_id = volume_id.replace('volume--', '')
    else:
        volume_id = volume_id.replace('volume-', '')
        volume_id = volume_id[0:volume_id.find('-')]
    return int(volume_id)


def _get_target_host(iscsi_string):
    """Retrieve target host."""
    if iscsi_string:
        return iscsi_string[0:iscsi_string.find(':')]
    elif iscsi_string is None or CONF.target_host:
        return CONF.target_host


def _get_target_port(iscsi_string):
    """Retrieve target port."""
    if iscsi_string:
        return iscsi_string[iscsi_string.find(':') + 1:]
    elif iscsi_string is None or CONF.target_port:
        return CONF.target_port
