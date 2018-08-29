# Copyright 2018 Nexenta Systems, Inc.
# All Rights Reserved.
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

from oslo_log import log
from oslo_utils import units
from six.moves import urllib

from manila.common import constants as common
from manila import exception
from manila.i18n import _
from manila.share import driver
from manila.share.drivers.nexenta.ns5 import jsonrpc
from manila.share.drivers.nexenta import options
from manila.share.drivers.nexenta import utils

VERSION = '1.0'
LOG = log.getLogger(__name__)
PATH_DELIMITER = '%2F'
ZFS_MULTIPLIER = 1.1  # ZFS quotas do not take metadata into account.


class NexentaNasDriver(driver.ShareDriver):
    """Nexenta Share Driver.

    Executes commands relating to Shares.
    API version history:
        1.0 - Initial version.
    """

    driver_prefix = 'nexenta'

    def __init__(self, *args, **kwargs):
        """Do initialization."""
        LOG.debug('Initializing Nexenta driver.')
        super(NexentaNasDriver, self).__init__(False, *args, **kwargs)
        self.configuration = kwargs.get('configuration')
        if self.configuration:
            self.configuration.append_config_values(
                options.nexenta_connection_opts)
            self.configuration.append_config_values(
                options.nexenta_nfs_opts)
            self.configuration.append_config_values(
                options.nexenta_dataset_opts)
        else:
            raise exception.BadConfigurationException(
                reason=_('Nexenta configuration missing.'))
        required_params = ['nas_host', 'user', 'password', 'pool', 'nfs_share']
        for param in required_params:
            if not getattr(self.configuration, 'nexenta_%s' % param):
                msg = 'Required parameter nexenta_%s is not provided.' % param
                raise exception.NexentaException(msg)

        self.nef = None
        self.verify_ssl = self.configuration.nexenta_ssl_cert_verify
        self.nef_protocol = self.configuration.nexenta_rest_protocol
        self.nef_host = self.configuration.nexenta_rest_host
        self.nas_host = self.configuration.nexenta_nas_host
        self.nef_port = self.configuration.nexenta_rest_port
        self.nef_user = self.configuration.nexenta_user
        self.nef_password = self.configuration.nexenta_password

        self.pool_name = self.configuration.nexenta_pool
        self.fs_prefix = self.configuration.nexenta_nfs_share

        self.storage_protocol = 'NFS'
        self.nfs_mount_point_base = self.configuration.nexenta_mount_point_base
        self.dataset_compression = (
            self.configuration.nexenta_dataset_compression)
        self.provisioned_capacity = 0

    @property
    def share_path(self):
        return '/'.join([self.pool_name, self.fs_prefix])

    @property
    def share_backend_name(self):
        if not hasattr(self, '_share_backend_name'):
            self._share_backend_name = None
            if self.configuration:
                self._share_backend_name = self.configuration.safe_get(
                    'share_backend_name')
            if not self._share_backend_name:
                self._share_backend_name = 'NexentaStor5'
        return self._share_backend_name

    def do_setup(self, context):
        """Any initialization the nexenta nas driver does while starting."""
        if self.nef_protocol == 'http':
            use_https = False
        else:
            use_https = True
        host = self.nef_host or self.nas_host
        self.nef = jsonrpc.NexentaJSONProxy(
            host, self.nef_port, self.nef_user,
            self.nef_password, use_https, self.pool_name, self.verify_ssl)

    def check_for_setup_error(self):
        """Verify that the volume for our folder exists.

        :raise: :py:exc:`LookupError`
        """
        url = 'storage/pools/{}'.format(self.pool_name)
        if not self.nef.get(url):
            raise LookupError(
                _("Pool {} does not exist in Nexenta Store appliance").format(
                    self.pool_name))

        url = 'nas/nfs?filesystem=%s' % urllib.parse.quote_plus(
            self.share_path)
        data = self.nef.get(url).get('data')
        if not (data and data[0].get('shareState') == 'online'):
            msg = (_('NFS share %(share)s is not accessible')
                   % {'share': self.share})
            raise exception.NexentaException(msg)
        self._get_provisioned_capacity()

    def _get_provisioned_capacity(self):
        url = 'storage/filesystems/%s' % urllib.parse.quote_plus(
            self.share_path)
        self.provisioned_capacity += self.nef.get(url)['referencedQuotaSize']

    def ensure_share(self, context, share, share_server=None):
        pass

    def create_share(self, context, share, share_server=None):
        """Create a share."""
        LOG.debug('Creating share: %s.', share['share_id'])
        dataset_name = self._get_dataset_name(share['share_id'])
        size = int(share['size'] * units.Gi * ZFS_MULTIPLIER)
        data = {
            'recordSize': 4 * units.Ki,
            'compressionMode': self.dataset_compression,
            'path': dataset_name,
            'referencedQuotaSize': size,
        }
        if not self.configuration.nexenta_thin_provisioning:
            data['referencedReservationSize'] = share['size'] * units.Gi

        url = 'storage/filesystems'
        self.nef.post(url, data)
        location = {
            'path': '{}:/{}'.format(
                self.nas_host, dataset_name),
            'id': share['id']
        }

        try:
            self._add_permission(share['share_id'])
        except exception.NexentaException as app_perm_exc:
            try:
                self.delete_share(None, share)
            except exception.NexentaException as exc:
                LOG.warning(
                    "Cannot destroy created filesystem: %(vol)s/%(folder)s, "
                    "exception: %(exc)s",
                    {'vol': self.pool_name, 'folder': '/'.join(
                        [self.fs_prefix, share['share_id']]), 'exc': exc})
            raise app_perm_exc
        dataset_url = 'storage/filesystems/%s' % (
            urllib.parse.quote_plus(dataset_name))
        dataset = self.nef.get(dataset_url)
        dataset_mount_point = dataset.get('mountPoint')
        dataset_ready = dataset.get('isMounted')
        if dataset_mount_point == 'none':
            hpr_url = 'hpr/activate'
            data = {'datasetName': dataset_name}
            self.nef.post(hpr_url, data)
            dataset = self.nef.get(dataset_url)
            dataset_mount_point = dataset.get('mountPoint')
        elif not dataset_ready:
            dataset_url = 'storage/filesystems/%s/mount' % (
                urllib.parse.quote_plus(dataset_name))
            self.nef.post(dataset_url)

        self.provisioned_capacity += share['size']
        return [location]

    def create_share_from_snapshot(self, context, share, snapshot,
                                   share_server=None):
        """Is called to create share from snapshot."""
        LOG.debug('Creating share from snapshot %s.', snapshot['snapshot_id'])

        fs_path = urllib.parse.quote_plus(self._get_dataset_name(
            snapshot['share_instance']['share_id']))
        url = ('storage/snapshots/%s/clone') % (
            '@'.join([fs_path, snapshot['snapshot_id']]))
        path = self._get_dataset_name(share['share_id'])
        size = int(share['size'] * units.Gi * ZFS_MULTIPLIER)
        data = {
            'targetPath': path,
            'referencedQuotaSize': size,
            'recordSize': 4 * units.Ki,
            'compressionMode': self.dataset_compression,
        }
        if not self.configuration.nexenta_thin_provisioning:
            data['referencedReservationSize'] = share['size'] * units.Gi
        self.nef.post(url, data)

        location = {
            'path': '{}:/{}/{}/{}'.format(self.nas_host, self.pool_name,
                                          self.fs_prefix, share['share_id'])
        }
        try:
            self._add_permission(share['share_id'])
        except exception.NexentaException:
            LOG.exception(
                "Failed to add permissions for %s", share['share_id'])
            try:
                self.delete_share(None, share)
            except exception.NexentaException:
                LOG.warning("Cannot destroy cloned filesystem: "
                            "%(vol)s/%(filesystem)s",
                            {'vol': self.pool_name,
                             'filesystem': '/'.join(
                                 [self.fs_prefix, share['share_id']])})
            raise

        self.provisioned_capacity += share['size']
        return [location]

    def _get_dataset_name(self, share_id):
        return str('/'.join([self.share_path, share_id]))

    def delete_share(self, context, share, share_server=None):
        """Delete a share."""
        LOG.debug('Deleting share: %s.', share['share_id'])
        path = self._get_dataset_name(share['share_id'])
        params = {'path': path}
        url = 'storage/filesystems?%s' % (
            urllib.parse.urlencode(params))
        fs_data = self.nef.get(url).get('data')
        if not fs_data:
            return
        params = {
            'force': 'true',
            'snapshots': 'true'
        }
        url = 'storage/filesystems/%s?%s' % (
            urllib.parse.quote_plus(path),
            urllib.parse.urlencode(params))
        try:
            self.nef.delete(url)
        except exception.NexentaException as ex:
            err = utils.ex2err(ex)
            if err['code'] == 'EEXIST':
                params = {'parent': path}
                url = 'storage/snapshots?%s' % (
                    urllib.parse.urlencode(params))
                snap_map = {}
                for snap in self.nef.get(url)['data']:
                    url = 'storage/snapshots/%s' % (
                        urllib.parse.quote_plus(snap['path']))
                    data = self.nef.get(url)
                    if data and data.get('clones'):
                        snap_map[data['creationTxg']] = snap['path']
                if snap_map:
                    snap = snap_map[max(snap_map)]
                    url = 'storage/snapshots/%s' % urllib.parse.quote_plus(
                        snap)
                    clone = self.nef.get(url)['clones'][0]
                    url = 'storage/filesystems/%s/promote' % (
                        urllib.parse.quote_plus(clone))
                    self.nef.post(url)
                params = {
                    'force': 'true',
                    'snapshots': 'true'
                }
                url = 'storage/filesystems/%s?%s' % (
                    urllib.parse.quote_plus(path),
                    urllib.parse.urlencode(params))
                self.nef.delete(url)
            else:
                raise ex
        self.provisioned_capacity -= share['size']

    def extend_share(self, share, new_size, share_server=None):
        """Extends a share."""
        LOG.debug(
            'Extending share: %(name)s to %(size)sG.', (
                {'name': share['share_id'], 'size': new_size}))
        self._set_quota(share['share_id'], new_size)
        self.provisioned_capacity += (new_size - share['size'])

    def shrink_share(self, share, new_size, share_server=None):
        """Shrinks size of existing share."""
        LOG.debug(
            'Shrinking share: %(name)s to %(size)sG.', {
                'name': share['share_id'], 'size': new_size})
        path = self._get_dataset_name(share['share_id'])
        url = 'storage/filesystems/' % path
        used = self.nef.get(url)['bytesUsedBySelf'] / units.Gi
        if used > new_size:
            raise exception.ShareShrinkingPossibleDataLoss(
                share_id=share['id'])
        self._set_quota(share['share_id'], new_size)
        self.provisioned_capacity += (share['size'] - new_size)

    def create_snapshot(self, context, snapshot, share_server=None):
        """Create a snapshot."""
        LOG.debug('Creating a snapshot of share: %s.',
                  snapshot['share']['share_id'])
        share_path = self._get_dataset_name(snapshot['share']['share_id'])
        url = 'storage/snapshots'
        data = {
            'path': '%s@%s' % (share_path, snapshot['snapshot_id'])
        }
        self.nef.post(url, data)

    def delete_snapshot(self, context, snapshot, share_server=None):
        """Delete a snapshot."""
        LOG.debug('Deleting a snapshot: %(shr_name)s@%(snap_name)s.', {
            'shr_name': snapshot['share']['share_id'],
            'snap_name': snapshot['snapshot_id']})
        path = '%s@%s' % (self._get_dataset_name(
            snapshot['share']['share_id']), snapshot['snapshot_id'])
        params = {'path': path}
        url = 'storage/snapshots?%s' % urllib.parse.urlencode(params)
        snap_data = self.nef.get(url).get('data')
        if not snap_data:
            return
        params = {'defer': 'true'}
        url = 'storage/snapshots/%s?%s' % (
            urllib.parse.quote_plus(path),
            urllib.parse.urlencode(params))
        self.nef.delete(url)

    def update_access(self, context, share, access_rules, add_rules,
                      delete_rules, share_server=None):
        """Update access rules for given share.

        Using access_rules list for both adding and deleting rules.
        :param context: The `context.RequestContext` object for the request
        :param share: Share that will have its access rules updated.
        :param access_rules: All access rules for given share. This list
        is enough to update the access rules for given share.
        :param add_rules: Empty List or List of access rules which should be
        added. access_rules already contains these rules. Not used by this
        driver.
        :param delete_rules: Empty List or List of access rules which should be
        removed. access_rules doesn't contain these rules. Not used by
        this driver.
        :param share_server: Data structure with share server information.
        Not used by this driver.
        """
        LOG.debug('Updating access to share %s.', share)
        rw_list = []
        ro_list = []
        security_contexts = []
        for rule in access_rules:
            if rule['access_type'].lower() != 'ip':
                msg = _('Only IP access type is supported.')
                raise exception.InvalidShareAccess(reason=msg)
            else:
                if rule['access_level'] == common.ACCESS_LEVEL_RW:
                    rw_list.append(rule['access_to'])
                else:
                    ro_list.append(rule['access_to'])

        def append_sc(addr_list, sc_type):
            for addr in addr_list:
                address_mask = addr.strip().split('/', 1)
                address = address_mask[0]
                ls = [{"allow": True, "etype": "network", "entity": address}]
                if len(address_mask) == 2:
                    try:
                        mask = int(address_mask[1])
                        if mask != 32:
                            ls[0]['mask'] = mask
                    except Exception:
                        raise exception.InvalidInput(
                            reason=_(
                                '<{}> is not a valid access parameter').format(
                                    addr))
                new_sc = {"securityModes": ["sys"]}
                new_sc[sc_type] = ls
                security_contexts.append(new_sc)

        append_sc(rw_list, 'readWriteList')
        append_sc(ro_list, 'readOnlyList')
        data = {"securityContexts": security_contexts}
        url = 'nas/nfs/' + PATH_DELIMITER.join(
            [self.pool_name, self.fs_prefix, share['share_id']])
        self.nef.put(url, data)

    def _set_quota(self, share_id, new_size):
        quota = int(new_size * units.Gi * ZFS_MULTIPLIER)
        data = {'referencedQuotaSize': quota}
        if not self.configuration.nexenta_thin_provisioning:
            data['referencedReservationSize'] = quota
        path = self._get_dataset_name(share_id)
        url = 'storage/filesystems/' % path
        self.nef.put(url, data)

    def _update_share_stats(self, data=None):
        super(NexentaNasDriver, self)._update_share_stats()
        total, free, allocated = self._get_capacity_info()
        compression = False if (self.dataset_compression == 'off') else True
        data = {
            'vendor_name': 'Nexenta',
            'storage_protocol': self.storage_protocol,
            'share_backend_name': self.share_backend_name,
            'nfs_mount_point_base': self.nfs_mount_point_base,
            'driver_version': VERSION,
            'snapshot_support': True,
            'create_share_from_snapshot_support': True,
            'pools': [{
                'pool_name': self.pool_name,
                'compression': compression,
                'total_capacity_gb': int(total),
                'free_capacity_gb': int(free),
                'reserved_percentage': (
                    self.configuration.reserved_share_percentage),
                'max_over_subscription_ratio': (
                    self.configuration.safe_get(
                        'max_over_subscription_ratio')),
                'thin_provisioning':
                    self.configuration.nexenta_thin_provisioning,
                'provisioned_capacity_gb': self.provisioned_capacity,
            }],
        }
        self._stats.update(data)

    def _get_capacity_info(self):
        """Calculate available space on the NFS share."""
        url = 'storage/pools/{}/filesystems/{}'.format(self.pool_name,
                                                       self.fs_prefix)
        data = self.nef.get(url)
        total = utils.bytes_to_gb(data['bytesAvailable'])
        allocated = utils.bytes_to_gb(data['bytesUsed'])
        free = total - allocated
        return total, free, allocated

    def _add_permission(self, share_name):
        """Share NFS filesystem on NexentaStor Appliance.

        :param share_name: relative filesystem name to be shared
        """
        LOG.debug(
            'Creating RW ACE for filesystem everyone on Nexenta Store '
            'for <%s> filesystem.', share_name)
        url = 'storage/pools/{}/filesystems/{}/acl'.format(
            self.pool_name, PATH_DELIMITER.join([self.fs_prefix, share_name]))
        data = {
            "type": "allow",
            "principal": "everyone@",
            "permissions": [
                "list_directory",
                "read_data",
                "add_file",
                "write_data",
                "add_subdirectory",
                "append_data",
                "read_xattr",
                "write_xattr",
                "execute",
                "delete_child",
                "read_attributes",
                "write_attributes",
                "delete",
                "read_acl",
                "write_acl",
                "write_owner",
                "synchronize",
            ],
            "flags": [
                "file_inherit",
                "dir_inherit",
            ],
        }
        self.nef.post(url, data)

        LOG.debug(
            'RW ACE for filesystem <%s> on Nexenta Store has been '
            'successfully created.', share_name)
