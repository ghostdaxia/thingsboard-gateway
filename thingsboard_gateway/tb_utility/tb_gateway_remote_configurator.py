#     Copyright 2023. ThingsBoard
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.

import os.path
from logging import getLogger
from time import sleep, time
from logging.config import dictConfig

from regex import fullmatch
from simplejson import dumps, load
from packaging import version

from thingsboard_gateway.gateway.tb_client import TBClient
from thingsboard_gateway.tb_utility.tb_handler import TBLoggerHandler

LOG = getLogger("service")


class RemoteConfigurator:
    DEFAULT_STATISTICS = {
        'enable': True,
        'statsSendPeriodInSeconds': 3600
    }

    def __init__(self, gateway, config):
        self._gateway = gateway
        self._config = config
        self._load_connectors_configuration()
        self._logs_configuration = self._load_logs_configuration()
        self.in_process = False
        self._active_connectors = []
        self._handlers = {
            'general_configuration': self._handle_general_configuration_update,
            'storage_configuration': self._handle_storage_configuration_update,
            'grpc_configuration': self._handle_grpc_configuration_update,
            'logs_configuration': self._handle_logs_configuration_update,
            'active_connectors': self._handle_active_connectors_update,
            'RemoteLoggingLevel': self._handle_remote_logging_level_update,
            r'(?=\D*\d?).*': self._handle_connector_configuration_update,
        }
        self._modifiable_static_attrs = {
            'logs_configuration': 'logs.json'
        }

        self._remote_gateway_version = None
        self._fetch_remote_gateway_version()

        LOG.info('Remote Configurator started')
        self.create_configuration_file_backup(config, "tb_gateway.json")

    @property
    def general_configuration(self):
        return self._config.get('thingsboard', {})

    @general_configuration.setter
    def general_configuration(self, config):
        self._config['thingsboard'].update(config)

    @property
    def storage_configuration(self):
        return self._config.get('storage', {})

    @storage_configuration.setter
    def storage_configuration(self, config):
        self._config['storage'].update(config)

    @property
    def grpc_configuration(self):
        return self._config.get('grpc', {})

    @grpc_configuration.setter
    def grpc_configuration(self, config):
        self._config.get('grpc', {}).update(config)

    @property
    def connectors_configuration(self):
        connectors = self._config.get('connectors', [])
        for connector in connectors:
            connector.pop('config_updated', None)
            connector.pop('config_file_path', None)
        return connectors

    def _fetch_remote_gateway_version(self):
        def callback(key, err):
            if err is not None:
                LOG.exception(err)
                self._remote_gateway_version = '0.0'

            try:
                self._remote_gateway_version = key['client']['Version']
            except KeyError as e:
                self._remote_gateway_version = '0.0'
                LOG.exception('Remote version number error (setting to 0.0): %s', e)

        self._gateway.tb_client.client.request_attributes(client_keys=['Version'], callback=callback)

    def _send_default_connectors_config(self):
        """
        If remote gateway version wasn't fetch (default set to '0.0'), remote configurator send all default
        connectors configs.
        """
        from thingsboard_gateway.gateway.tb_gateway_service import DEFAULT_CONNECTORS

        # remote gateway version fetching in __init__ method (_fetch_remote_gateway_version)
        LOG.info('Waiting for remote gateway version...')

        try_count = 1
        while not self._remote_gateway_version and try_count <= 3:
            try_count += 1
            sleep(1)

        need_update_configs = version.parse(self._gateway.version.get('current_version', '0.0')) > version.parse(
            str(self._remote_gateway_version))

        if need_update_configs:
            default_connectors_configs_folder_path = self._gateway.get_config_path() + 'default-configs/'

            for (connector_type, _) in DEFAULT_CONNECTORS.items():
                connector_filename = connector_type + '.json'
                try:
                    with open(default_connectors_configs_folder_path + connector_filename, 'r') as file:
                        config = load(file)
                        self._gateway.tb_client.client.send_attributes({connector_type.upper() + '_DEFAULT_CONFIG': config})
                        LOG.debug('Default config for %s connector sent.', connector_type)
                except FileNotFoundError:
                    LOG.error('Default config file for %s connector not found! Passing...', connector_type)

    def _get_active_connectors(self):
        return [connector['name'] for connector in self.connectors_configuration]

    def _get_general_config_in_local_format(self):
        """
        Method returns general configuration in format that should be used only for local files
        !!!Don't use it for sending data to TB (use `_get_general_config_in_remote_format` instead)!!!
        """

        connectors_config = [
            {'type': connector['type'], 'name': connector['name'], 'configuration': connector['configuration']} for
            connector in self.connectors_configuration]

        return {
            'thingsboard': self.general_configuration,
            'storage': self.storage_configuration,
            'grpc': self.grpc_configuration,
            'connectors': connectors_config
        }

    def _get_general_config_in_remote_format(self):
        """
        Method returns general configuration in format that should be used only for sending configuration to the server.
        !!!Don't use it for saving data to conf files (use `_get_general_config_in_local_format`)!!!
        """

        stat_conf_path = self.general_configuration['statistics'].get('configuration')
        commands = []
        if stat_conf_path:
            with open(self._gateway.get_config_path() + stat_conf_path, 'r') as file:
                commands = load(file)
        config = self.general_configuration
        config.update(
            {
                'statistics': {
                    'enable': self.general_configuration['statistics']['enable'],
                    'statsSendPeriodInSeconds': self.general_configuration['statistics']['statsSendPeriodInSeconds'],
                    'configuration': self.general_configuration['statistics'].get('configuration'),
                    'commands': commands
                }
            }
        )
        return config

    def send_current_configuration(self):
        """
        Calling manually only on RemoteConfigurator init (TbGatewayService.__init_remote_configuration method)
        When Gateway started, sending all configs for states synchronizing
        """

        LOG.debug('Sending all configurations (init)')
        self._gateway.tb_client.client.send_attributes(
            {'general_configuration': self._get_general_config_in_remote_format()})
        self._gateway.tb_client.client.send_attributes({'storage_configuration': self.storage_configuration})
        self._gateway.tb_client.client.send_attributes({'grpc_configuration': self.grpc_configuration})
        self._gateway.tb_client.client.send_attributes(
            {'logs_configuration': {**self._logs_configuration, 'ts': int(time() * 1000)}})
        self._gateway.tb_client.client.send_attributes({'active_connectors': self._get_active_connectors()})
        self._send_default_connectors_config()
        self._gateway.tb_client.client.send_attributes({'Version': self._gateway.version.get('current_version', '0.0')})
        for connector in self.connectors_configuration:
            self._gateway.tb_client.client.send_attributes(
                {connector['name']: {**connector, 'logLevel': connector['configurationJson'].get('logLevel', 'INFO'),
                                     'ts': int(time() * 1000)}})

    def _load_connectors_configuration(self):
        for (_, connector_list) in self._gateway.connectors_configs.items():
            for connector in connector_list:
                for general_connector_config in self.connectors_configuration:
                    if general_connector_config['name'] == connector['name']:
                        config = connector.pop('config')[general_connector_config['configuration']]
                        general_connector_config.update(connector)
                        general_connector_config['configurationJson'] = config

    def _load_logs_configuration(self):
        """
        Calling only on RemoteConfigurator init (__init__ method)
        """

        try:
            with open(self._gateway.get_config_path() + 'logs.json', 'r') as logs:
                return load(logs)
        except Exception as e:
            LOG.exception(e)
            return {}

    def process_config_request(self, config):
        if not self.in_process:
            LOG.debug('Got config update request: %s', config)

            self.in_process = True

            try:
                for attr_name in config.keys():
                    if 'deleted' in attr_name:
                        continue

                    request_config = config[attr_name]
                    if not self._is_modified(attr_name, request_config):
                        continue

                    for (name, func) in self._handlers.items():
                        if fullmatch(name, attr_name):
                            func(request_config)
                            break
            except (KeyError, AttributeError):
                LOG.error('Unknown attribute update name (Available: %s)', ', '.join(self._handlers.keys()))
            finally:
                self.in_process = False
        else:
            LOG.error("Remote configuration is already in processing")

    # HANDLERS ---------------------------------------------------------------------------------------------------------
    def _handle_general_configuration_update(self, config):
        """
        General configuration update handling in 5 steps:
        1. Checking if connection changed (host, port, QoS, security or provisioning sections);
        2. Checking if statistics collecting changed;
        3. Checking if device filtering changed;
        4. Checking if Remote Shell on/off state changed;
        5. Updating other params (regardless of whether they have changed):
            a. maxPayloadSizeBytes;
            b. minPackSendDelayMS;
            c. minPackSizeToSend;
            d. checkConnectorsConfigurationInSeconds;
            f. handleDeviceRenaming.

        If config from steps 1-4 changed:
        True -> applying new config with related objects creating (old objects will remove).
        """

        LOG.info('Processing general configuration update')

        LOG.info('--- Checking connection configuration changes...')
        if config['host'] != self.general_configuration['host'] or config['port'] != self.general_configuration[
            'port'] or config['security'] != self.general_configuration['security'] or config.get('provisioning',
                                                                                                  {}) != self.general_configuration.get(
                'provisioning', {}) or config['qos'] != self.general_configuration['qos']:
            LOG.info('---- Connection configuration changed. Processing...')
            success = self._apply_connection_config(config)
            if not success:
                config.update(self.general_configuration)
        else:
            LOG.info('--- Connection configuration not changed.')

        LOG.info('--- Checking statistics configuration changes...')
        changed = self._check_statistics_configuration_changes(config.get('statistics', self.DEFAULT_STATISTICS))
        if changed:
            LOG.info('---- Statistics configuration changed. Processing...')
            success = self._apply_statistics_config(config.get('statistics', self.DEFAULT_STATISTICS))
            if not success:
                config['statistics'].update(self.general_configuration['statistics'])
        else:
            LOG.info('--- Statistics configuration not changed.')

        LOG.info('--- Checking device filtering configuration changes...')
        if config.get('deviceFiltering') != self.general_configuration.get('deviceFiltering'):
            LOG.info('---- Device filtering configuration changed. Processing...')
            success = self._apply_device_filtering_config(config)
            if not success:
                config['deviceFiltering'].update(self.general_configuration['deviceFiltering'])
        else:
            LOG.info('--- Device filtering configuration not changed.')

        LOG.info('--- Checking Remote Shell configuration changes...')
        if config.get('remoteShell') != self.general_configuration.get('remoteShell'):
            LOG.info('---- Remote Shell configuration changed. Processing...')
            success = self._apply_remote_shell_config(config)
            if not success:
                config['remoteShell'].update(self.general_configuration['remoteShell'])
        else:
            LOG.info('--- Remote Shell configuration not changed.')

        LOG.info('--- Checking other configuration parameters changes...')
        self._apply_other_params_config(config)

        LOG.info('--- Saving new general configuration...')
        self.general_configuration = config
        self._gateway.tb_client.client.send_attributes({'general_configuration': self.general_configuration})
        self._cleanup()
        with open(self._gateway.get_config_path() + "tb_gateway.json", "w",
                  encoding="UTF-8") as file:
            file.writelines(dumps(self._get_general_config_in_local_format(), indent='  '))

    def _handle_storage_configuration_update(self, config):
        LOG.debug('Processing storage configuration update...')

        old_event_storage = self._gateway._event_storage
        try:
            storage_class = self._gateway.event_storage_types[config["type"]]
            self._gateway._event_storage = storage_class(config)
        except Exception as e:
            LOG.error('Something went wrong with applying the new storage configuration. Reverting...')
            LOG.exception(e)
            self._gateway._event_storage = old_event_storage
        else:
            self.storage_configuration = config
            with open(self._gateway.get_config_path() + "tb_gateway.json", "w", encoding="UTF-8") as file:
                file.writelines(dumps(self._get_general_config_in_local_format(), indent='  '))
            self._gateway.tb_client.client.send_attributes({'storage_configuration': self.storage_configuration})

            LOG.info('Processed storage configuration update successfully')

    def _handle_grpc_configuration_update(self, config):
        LOG.debug('Processing GRPC configuration update...')
        if config != self.grpc_configuration:
            try:
                self._gateway.init_grpc_service(config)
                for connector_name in self._gateway.available_connectors:
                    self._gateway.available_connectors[connector_name].close()
                self._gateway.load_connectors(self._get_general_config_in_local_format())
                self._gateway.connect_with_connectors()
            except Exception as e:
                LOG.error('Something went wrong with applying the new GRPC configuration. Reverting...')
                LOG.exception(e)
                self._gateway.init_grpc_service(self.grpc_configuration)
                for connector_name in self._gateway.available_connectors:
                    self._gateway.available_connectors[connector_name].close()
                self._gateway.load_connectors(self._get_general_config_in_local_format())
                self._gateway.connect_with_connectors()
            else:
                self.grpc_configuration = config
                with open(self._gateway.get_config_path() + "tb_gateway.json", "w", encoding="UTF-8") as file:
                    file.writelines(dumps(self._get_general_config_in_local_format(), indent='  '))
                self._gateway.tb_client.client.send_attributes({'grpc_configuration': self.grpc_configuration})

                LOG.info('Processed GRPC configuration update successfully')

    def _handle_logs_configuration_update(self, config):
        global LOG
        LOG.debug('Processing logs configuration update...')
        try:
            LOG = getLogger('service')
            logs_conf_file_path = self._gateway.get_config_path() + 'logs.json'

            dictConfig(config)
            LOG = getLogger('service')
            self._gateway.remote_handler = TBLoggerHandler(self._gateway)
            self._gateway.remote_handler.activate(self._gateway.main_handler.level)
            self._gateway.main_handler.setTarget(self._gateway.remote_handler)
            LOG.addHandler(self._gateway.remote_handler)

            with open(logs_conf_file_path, 'w') as logs:
                logs.write(dumps(config, indent='  '))

            LOG.debug("Logs configuration has been updated.")
            self._gateway.tb_client.client.send_attributes({'logs_configuration': config})
        except Exception as e:
            LOG.error("Remote logging configuration is wrong!")
            LOG.exception(e)

    def _handle_active_connectors_update(self, config):
        LOG.debug('Processing active connectors configuration update...')

        has_changed = False
        for_deletion = []
        for active_connector_name in self._gateway.available_connectors:
            if active_connector_name not in config:
                try:
                    self._gateway.available_connectors[active_connector_name].close()
                    for_deletion.append(active_connector_name)
                    has_changed = True
                except Exception as e:
                    LOG.exception(e)

        if has_changed:
            for name in for_deletion:
                self._gateway.available_connectors.pop(name)

            self._delete_connectors_from_config(config)
            with open(self._gateway.get_config_path() + 'tb_gateway.json', 'w') as file:
                file.writelines(dumps(self._get_general_config_in_local_format(), indent='  '))
            self._active_connectors = config

        self._gateway.tb_client.client.send_attributes({'active_connectors': config})

    def _handle_connector_configuration_update(self, config):
        """
        Expected the following data structure:
        {
            "name": "Mqtt Broker Connector",
            "type": "mqtt",
            "configuration": "mqtt.json",
            "logLevel": "INFO",
            "key?type=>grpc": "auto",
            "class?type=>custom": "",
            "configurationJson": {
                ...
            }
        }
        """

        LOG.debug('Processing connectors configuration update...')

        try:
            config_file_name = config['configuration']

            found_connectors = list(filter(lambda item: item['name'] == config['name'], self.connectors_configuration))
            if not found_connectors:
                connector_configuration = {'name': config['name'], 'type': config['type'],
                                           'configuration': config_file_name}
                if config.get('key'):
                    connector_configuration['key'] = config['key']

                if config.get('class'):
                    connector_configuration['class'] = config['class']

                with open(self._gateway.get_config_path() + config_file_name, 'w') as file:
                    config['configurationJson'].update({'logLevel': config['logLevel'], 'name': config['name']})
                    self.create_configuration_file_backup(config, config_file_name)
                    file.writelines(dumps(config['configurationJson'], indent='  '))

                self.connectors_configuration.append(connector_configuration)
                with open(self._gateway.get_config_path() + 'tb_gateway.json', 'w') as file:
                    file.writelines(dumps(self._get_general_config_in_local_format(), indent='  '))

                self._gateway.load_connectors(self._get_general_config_in_local_format())
                self._gateway.connect_with_connectors()
            else:
                found_connector = found_connectors[0]
                changed = False

                config_file_path = self._gateway.get_config_path() + config_file_name
                if os.path.exists(config_file_path):
                    with open(config_file_path, 'r') as file:
                        connector_config_data = load(file)
                        config_hash = hash(str(connector_config_data))

                    if config_hash != hash(str(config['configurationJson'])):
                        self.create_configuration_file_backup(connector_config_data, config_file_name)
                        changed = True

                connector_configuration = None
                if found_connector.get('name') != config['name'] or found_connector.get('type') != config[
                    'type'] or found_connector.get('class') != config.get('class') or found_connector.get(
                    'key') != config.get('key') or found_connector.get('configurationJson', {}).get(
                        'logLevel') != config.get('logLevel'):
                    changed = True
                    connector_configuration = {'name': config['name'], 'type': config['type'],
                                               'configuration': config_file_name}

                    if config.get('key'):
                        connector_configuration['key'] = config['key']

                    if config.get('class'):
                        connector_configuration['class'] = config['class']

                    found_connector.update(connector_configuration)

                if changed:
                    with open(self._gateway.get_config_path() + config_file_name, 'w') as file:
                        config['configurationJson'].update({'logLevel': config['logLevel'], 'name': config['name']})
                        file.writelines(dumps(config['configurationJson'], indent='  '))

                    if connector_configuration is None:
                        connector_configuration = found_connector

                    self._gateway.available_connectors[connector_configuration['name']].close()
                    self._gateway.available_connectors.pop(connector_configuration['name'])

                    self._gateway.load_connectors(self._get_general_config_in_local_format())
                    self._gateway.connect_with_connectors()

            self._gateway.tb_client.client.send_attributes({config['name']: config})
        except Exception as e:
            LOG.exception(e)

    def _handle_remote_logging_level_update(self, config):
        self._gateway.tb_client.client.send_attributes({'RemoteLoggingLevel': config})

    # HANDLERS SUPPORT METHODS -----------------------------------------------------------------------------------------
    def _apply_connection_config(self, config) -> bool:
        apply_start = time() * 1000
        old_tb_client = self._gateway.tb_client
        try:
            old_tb_client.disconnect()

            new_tb_client = TBClient(config, old_tb_client.get_config_folder_path())

            connection_state = False
            while not connection_state:
                for client in (new_tb_client, old_tb_client):
                    client.connect()
                    while time() * 1000 - apply_start >= 1000 and not connection_state:
                        connection_state = client.is_connected()
                        sleep(.1)

                    if connection_state:
                        self._gateway.tb_client = client
                        self._gateway.subscribe_to_required_topics()
                        return True
        except Exception as e:
            LOG.exception(e)
            self._revert_connection()
            return False

    def _revert_connection(self):
        try:
            LOG.info("Remote general configuration will be restored.")
            self._gateway.tb_client.disconnect()
            self._gateway.tb_client.stop()
            self._gateway.tb_client = TBClient(self.general_configuration, self._gateway.get_config_path())
            self._gateway.tb_client.connect()
            self._gateway.subscribe_to_required_topics()
            LOG.debug("%s connection has been restored", str(self._gateway.tb_client.client))
        except Exception as e:
            LOG.exception("Exception on reverting configuration occurred:")
            LOG.exception(e)

    def _apply_statistics_config(self, config) -> bool:
        try:
            commands = config.get('commands', [])
            if commands:
                statistics_conf_file_name = self.general_configuration['statistics'].get('configuration',
                                                                                         'statistics.json')
                if statistics_conf_file_name is None:
                    statistics_conf_file_name = 'statistics.json'

                with open(self._gateway.get_config_path() + statistics_conf_file_name, 'w') as file:
                    file.writelines(dumps(commands, indent='  '))
                config['configuration'] = statistics_conf_file_name

            self._gateway.init_statistics_service(config)
            self.general_configuration['statistics'] = config
            return True
        except Exception as e:
            LOG.error('Something went wrong with applying the new statistics configuration. Reverting...')
            LOG.exception(e)
            self._gateway.init_statistics_service(
                self.general_configuration.get('statistics', {'enable': True, 'statsSendPeriodInSeconds': 3600}))
            return False

    def _apply_device_filtering_config(self, config):
        try:
            self._gateway.init_device_filtering(config.get('deviceFiltering', {'enable': False}))
            return True
        except Exception as e:
            LOG.error('Something went wrong with applying the new device filtering configuration. Reverting...')
            LOG.exception(e)
            self._gateway.init_device_filtering(
                self.general_configuration.get('deviceFiltering', {'enable': False}))
            return False

    def _apply_remote_shell_config(self, config):
        try:
            self._gateway.init_remote_shell(config.get('remoteShell'))
            return True
        except Exception as e:
            LOG.error('Something went wrong with applying the new Remote Shell configuration. Reverting...')
            LOG.exception(e)
            self._gateway.init_remote_shell(self.general_configuration.get('remoteShell'))
            return False

    def _apply_other_params_config(self, config):
        self._gateway.config['thingsboard'].update(config)

    def _delete_connectors_from_config(self, connector_list):
        self._config['connectors'] = list(
            filter(lambda connector: connector['name'] in connector_list, self.connectors_configuration))

    def _check_statistics_configuration_changes(self, config):
        general_statistics_config = self.general_configuration.get('statistics', self.DEFAULT_STATISTICS)
        if config['enable'] != general_statistics_config['enable'] or config['statsSendPeriodInSeconds'] != \
                general_statistics_config['statsSendPeriodInSeconds']:
            return True

        commands = []
        if general_statistics_config.get('configuration'):
            with open(self._gateway.get_config_path() + general_statistics_config['configuration'], 'r') as file:
                commands = load(file)

        if config.get('commands', []) != commands:
            return True

        return False

    def _cleanup(self):
        self.general_configuration['statistics'].pop('commands')

    def _is_modified(self, attr_name, config):
        try:
            file_path = config.get('configuration') or self._modifiable_static_attrs.get(attr_name)
        except AttributeError:
            file_path = None

        # if there is no file path that means that it is RemoteLoggingLevel or active_connectors attribute update
        # in this case, we have to update the configuration without TS compare
        if file_path is None:
            return True

        try:
            file_path = self._gateway.get_config_path() + file_path
            if config.get('ts', 0) <= int(os.path.getmtime(file_path) * 1000):
                return False
        except OSError:
            LOG.warning('File %s not exist', file_path)

        return True

    def create_configuration_file_backup(self, config_data, config_file_name):
        backup_folder_path = self._gateway.get_config_path() + "backup"
        if not os.path.exists(backup_folder_path):
            os.mkdir(backup_folder_path)

        backup_file_path = backup_folder_path + os.path.sep + config_file_name + "_backup_" + str(int(time()))
        with open(backup_file_path, "w") as backup_file:
            LOG.debug(f"Backup file created for configuration file {config_file_name} in {backup_file_path}")
            backup_file.writelines(dumps(config_data, indent='  '))
            
