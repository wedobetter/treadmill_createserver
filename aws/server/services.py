import dns.resolver
import logging
from os import environ
from random import choice
import requests
from requests_kerberos import HTTPKerberosAuth
from treadmill.infra import connection
from pprint import pprint

_LOGGER = logging.getLogger(__name__)
_KERBEROS_AUTH = HTTPKerberosAuth()

API_VERSION = '2.28'

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(message)s"))
_LOGGER.addHandler(console_handler)


class IPAClient():
    ''' Interfaces with freeIPA API to register and deregister hosts '''
    
    def __init__(self):
        self.cell_name = environ.get('TREADMILL_CELL')
        self.domain = environ.get('TREADMILL_DNS_DOMAIN')
        self.ipa_cert_location = '/etc/ipa/ca.crt'
        # Strip trailing period as it breaks SSL
        self.ipa_server_hostn = self.get_ipa_server_from_dns(self.domain)[:-1]
        self.ipa_srv_address = 'https://{}/ipa'.format(self.ipa_server_hostn)
        self.ipa_srv_api_address = '{}/session/json'.format(
            self.ipa_srv_address)
        self.referer = {'referer': self.ipa_srv_address}
        
    def get_ipa_server_from_dns(self, tm_dns_domain):
        ''' Looks up random IPA server from DNS SRV records '''
        raw_results = [
            result.to_text() for result in
            dns.resolver.query('_kerberos._tcp.{}'.format(tm_dns_domain),
                               'SRV')
            ]
        return choice(raw_results).split()[-1]
        
    def _post(self, payload=None, auth=_KERBEROS_AUTH):
        ''' Submits formatted JSON to IPA server.
            Uses requests_kerberos module for Kerberos authentication with IPA.
        '''
        response = requests.post(self.ipa_srv_api_address,
                                 json=payload,
                                 auth=auth,
                                 headers=self.referer,
                                 verify=self.ipa_cert_location)
        return response
        
    def enroll_ipa_host(self, hostname):
        ''' Add new host to IPA server'''
        payload = {'method': 'host_add',
                   'params': [[hostname],
                              {'force': True,
                               'random': True,
                               'version': API_VERSION,
                               }
                              ],
                   'id': 0}
        r = self._post(payload)
        try:
            return r.json()
        except Exception as e:
            _LOGGER.error(r, str(e))
            
    def unenroll_ipa_host(self, hostname):
        ''' Delete host from IPA server'''
        payload = {'method': 'host_del',
                   'params': [[hostname],
                              {'version': API_VERSION
                               }
                              ],
                   'id': 0}
        r = self._post(payload)
        try:
            if r.json()['error']:
                raise  
        except Exception as e:
            _LOGGER.error(r, str(e))
            
    def get_ipa_hosts(self):
        ''' Retrieve all host records from IPA server '''
        payload = {'method': 'host_find',
                   'params': [[''],
                              {'version': API_VERSION}
                              ],
                   'id': 0}
        r = self._post(payload)
        try:
            return r.json()
        except Exception as e:
            _LOGGER.error(r, str(e))

    def get_ipa_host(self, hostname):
        ''' Retrieve host record from IPA server '''
        payload = {'method': 'host_find',
                   'params': [[hostname],
                              {'version': API_VERSION},
                              ],
                   'id': 0}
        r = self._post(payload)
        try:
            return r.json()
        except Exception as e:
            _LOGGER.error(r, str(e))


class AWSClient():
    ''' Interfaces with TM AWS connection '''

    def __init__(self):
        self.ec2_conn = connection.Connection()

    def create_instance(self, manifest):
        ''' Add new instance to AWS using properties in manifest '''
        user_data = self.render_manifest(manifest)
        _instances = self.ec2_conn.run_instances(
            ImageId=manifest['image_id'],
            MinCount=manifest['count'],
            MaxCount=manifest['count'],
            InstanceType=manifest['instance_type'],
            KeyName=manifest['key'],
            UserData=user_data,
            NetworkInterfaces=[{
                'DeviceIndex': 0,
                'SubnetId': manifest['subnet_id'],
                'Groups': [manifest['secgroup_ids']]
            }],
        )
        _LOGGER.info('New instances: {}'.format(pprint(_instances)))

    def delete_instance(self, hostname):
        ''' Delete host from IPA server'''
        instances = self.get_instance_by_hostname(hostname)
        for instance in instances:
            _LOGGER.info('Delete {}'.format(instance['InstanceId']))

    def get_instance_by_hostname(self, hostname):
        ''' Get host details from AWS
            Returns list of instances that match hostname
            AWS returns instances in nested list- flatten to simple list
        '''
        filters = [{'Name': 'tag:Name', 'Values': [hostname]}]
        reservations = [
            x['Instances'] for x in
            self.ec2_conn.describe_instances(Filters=filters)['Reservations']
            ]
        return [result
                for reservation in reservations
                for result in reservation]

    def render_manifest(self, manifest):
        template = '''#!/bin/bash \nhostnamectl set-hostname {fqdn}\n 
yum install -y ipa-client-install\n
ipa-client-install \
--server={fqdn} \
--domain={domain} \ 
--realm={realm}  \
--password='{otp}'\
--mkhomedir \
--no-ntp \
--unattended'''.format(fqdn=manifest['fqdn'],
                       domain=manifest['domain'],
                       realm=manifest['realm'],
                       otp=manifest['otp'],
                       )
        return template
