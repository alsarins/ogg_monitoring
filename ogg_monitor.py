#!/usr/bin/env python
# pylint: disable=too-many-lines
"""
Script for parsing Oracle GoldenGate output and sending results to ZABBIX (monitoring system)
Tested on python2.7
Author: Mamaev Sergei <smamaev@mail.ru>
"""

import sys
import os
import argparse
import subprocess
import time
import fcntl # pylint: disable=import-error
import platform
import logging
import logging.handlers
import random
import locale
import ConfigParser
import re
import hashlib
import json

# single global variable for ease of editing
VERSION = '0.4.2'

def get_shell_output(cmd, use_shell, use_stderr):
    """ run shell script and return it's output """
    cmd_output = subprocess.check_output(cmd, shell=use_shell, stderr=use_stderr)
    return cmd_output

def isused(filename):
    """ returns true if file is in use by any process """
    if not os.path.isfile(filename):
        return False

    try:
        devnull = open(os.devnull, 'wb') # pylint: disable=invalid-name
        cmd = 'fuser ' + filename + '| wc -w'
        out = get_shell_output(cmd, use_shell=True, use_stderr=devnull)
        #subprocess.check_output(cmd, shell=True, stderr=devnull)
    except Exception as e: # pylint: disable=broad-except,invalid-name
        print(str(e))
        return True

    if out.strip() == '0':
        # print('no process found. Safe to continue')
        return False
    else:
        print('file ' + filename + ' is used by another process(es). Use command: fuser ' + filename + ' to investigate issue')
        return True

    # default value:
    return False

def log_debug(_ogg, strn):
    """ log debug messages """
    _ogg.logger.debug(_ogg.logging_prefix + strn)

def log_info(_ogg, strn):
    """ log warning messages """
    _ogg.logger.info(_ogg.logging_prefix + strn)

def log_warn(_ogg, strn):
    """ log warning messages """
    _ogg.logger.warn(_ogg.logging_prefix + strn)

def log_error(_ogg, strn):
    """ log error messages """
    _ogg.logger.error(_ogg.logging_prefix + strn)


class OggZabbix(object): # pylint: disable=too-many-instance-attributes
    """ class for goldengate monitoring object """
    def __init__(self):
        # Constants
        self.zbx_discovery_key = 'ogg.process.discovery'

        # OS version and hostname
        self.platform_type = platform.system()
        self.short_hostname = platform.node().split('.', 1)[0]

        # Variables default values
        if self.platform_type == 'Windows':
            self.logging_dir = 'c:\\temp'
            self.lock_file = 'c:\\temp\\zabbix_ogg_python.lock'
            self.zabbix_sender = 'c:\\temp\\zabbix_sender'
        else:
            self.logging_dir = '/tmp'
            self.lock_file = '/tmp/zabbix_ogg_python.lock'
            self.zabbix_sender = '/tmp/zabbix_sender'
            locale.setlocale(locale.LC_ALL, 'en_US')
        self.zabbix_servers = ''
        self.environment = 'test'
        self.use_hostname_for_zabbix = 'YES'
        # use_hostname_for_zabbix description:
        # YES - use current hostname for generating zabbix host, e.g. OGG_SRV12345.DOMAIN.RU_7809
        # NO - when try to use metadata from manager config if found, e.g. OGG_MYREPLICATION1_7809.
        # if metadata not found, then fallback to use hostname
        # manager config should have a unique line like: COMMENT OGG_INSTANCE_ID=MyReplicationl
        self.args = None
        # logging properties
        self.logger = None
        self.logging_file = None
        self.logging_prefix = ''
        self.debug_mode = False
        self.logging_level = logging.INFO

        self.env_vars = None
        self.ogg_architecture = 'classic'
        self.ogg_cmd_prefix = ''
        self.ogg_script = ''
        self.ogg_console = ''
        self.utc_string = ''
        self.ogg_script_output = None
        self.ogg_script_outlist = []
        self.ogg_version = ''
        self.ogg_database = ''
        self.ogg_total_memory = 0
        self.process_dictionary = {}
        self.ogg_zabbix_list = []
        self.zbx_hostname = ''
        self.json_string = ''
        self.mutex = None

        # microservices parameters
        self.sm_port = ''
        self.sm_security_enabled = False
        self.json_cmdb_file = None

    def parse_arguments(self):
        """ Parsing input parameters and set up variables """
        parser = argparse.ArgumentParser(description='Sending GoldenGate metrics to Zabbix')
        parser.add_argument('env', nargs="?", help='Environment of the current GoldenGate instance', choices=['prod', 'test', 'preprod', 'dev'])
        parser.add_argument('srv', nargs="?", help='DNS name or IP address of the Zabbix servers. Multiple hosts allowed delimited with comma')
        parser.add_argument('-d', '-debug', help='Enable debug mode (more info in log file)', choices=['enable', 'disable'])
        parser.add_argument('-v', '-version', help='Print version and exit', action='version', version='Version '+ VERSION)
        parser.add_argument('-l', '-logfile', help='Name of file for logging', metavar='FILE')
        parser.add_argument('-c', '-configfile', help='Name of config file for parameters', metavar='CONFIGFILE')
        parser.add_argument('-z', '-zabbix-sender', nargs="?", help='Path to zabbix_sender binary, e.g /bin/zabbix_sender', metavar='ZBX_SENDER')
        parser.add_argument('-j', '-jsonfile', nargs="?", help='Full path to file for exporting JSON data for cmdb, e.g /tmp/cmdb_file.json', metavar='JSONFILE')

        self.args = parser.parse_args()

        if (len(sys.argv) == 1) and (self.args.configfile is None):
            parser.print_help()
            sys.exit(0)

        # assign variables with arguments defined in command line
        if self.args.zabbix_sender is not None:
            self.zabbix_sender = self.args.zabbix_sender

        if self.args.env is not None:
            self.environment = self.args.env

        if self.args.srv is not None:
            self.zabbix_servers = self.args.srv

        # assign json file anyway
        self.json_cmdb_file = self.args.jsonfile

        #zbx_servers_list = zbx_servers.split(',')

        return True

    def read_inifile(self):
        """ read variables from configuration file """
        config = ConfigParser.ConfigParser()
        try:
            with open(self.args.configfile) as fp: # pylint: disable=invalid-name
                config.read(self.args.configfile)
                self.logging_dir = config.get('common', 'LOG_FILE_DIR')
                self.lock_file = config.get('common', 'LOCK_FILE')

                # give priority to command line over ini file
                # only read parameters if command line arguments are not defined
                if self.args.zabbix_sender is None:
                    self.zabbix_sender = config.get('common', 'ZBX_SENDER')
                if self.args.env is None:
                    self.environment = config.get('common', 'ENVIRONMENT')
                if self.args.srv is None:
                    self.zabbix_servers = config.get('common', 'ZABBIX_SERVERS')

                try:
                    # parameter might be unavailable in ini file, it's valid
                    if self.args.jsonfile is None:
                        self.json_cmdb_file = config.get('common', 'EXPORT_JSON_FOR_CDMDB')
                except Exception as e: # pylint: disable=broad-except,invalid-name
                    self.json_cmdb_file = None
                try:
                    # parameter might be unavailable in ini file, it's valid
                    self.use_hostname_for_zabbix = config.get('common', 'USE_HOSTNAME_FOR_ZABBIX').upper()
                except Exception as e:  # pylint: disable=broad-except,invalid-name
                    # fallback to default behavior - use host name
                    self.use_hostname_for_zabbix = 'YES'
                if self.use_hostname_for_zabbix.upper() not in ('YES', 'NO'):
                    print('wrong value for USE_HOSTNAME_FOR_ZABBIX in ini file. Only YES or NO allowed')
                    sys.exit(1)
            fp.close()
        except Exception as e:  # pylint: disable=broad-except,invalid-name
            print('Error reading the file {0} : {1}'.format(self.args.configfile, e))
            sys.exit(1)

        return True

    def log_and_debug(self):
        """ enable logging and debug level according to parameters """
        # generate unique log file name for every OGG
        logging_postfix = '.' + hashlib.md5(os.environ.get('OGG_HOME')).hexdigest()

        if self.args.logfile is not None:
            self.logging_file = self.args.logfile + logging_postfix
        elif self.args.configfile is not None:
            self.logging_file = self.logging_dir + '/zbx_ogg_monitor.log' + logging_postfix

        if self.args.debug == 'enable':
            self.debug_mode = True
            self.logging_level = logging.DEBUG
        else:
            self.debug_mode = False
            self.logging_level = logging.INFO

        self.logger = logging.getLogger(__name__)

        # check --logfile parameter: create and delete temporary file in this location
        if self.logging_file is not None:
            if isused(self.logging_file):
                #if file exists, then another process use it.
                #exit now, because we dont want to overwrite active log file
                print('Log file ' + self.logging_file + ' is busy. Exit now')
                sys.exit(1)
            try:
                #generate random file name and check write permissions with the destination directory
                probe_file_name = self.logging_file + '.' + str(random.randint(1000, 9999)) + '.delete_me'
                f = open(probe_file_name, 'w') # pylint: disable=invalid-name
                f.close()
                os.remove(probe_file_name)
            except Exception as e: # pylint: disable=broad-except,invalid-name
                print('ERROR: Invalid logging file name: ' + self.logging_file + '\nCheck the path or directory permissions')
                print(str(e))
                #last chance to clear temporary file, if it survived somehow
                try:
                    os.remove(probe_file_name)
                except OSError:
                    print('ERROR: can not delete temporary file ' + probe_file_name + ' Delete it manually!')
                sys.exit(1)

            print('Output redirected to the log file : ' + self.logging_file)
            logging.basicConfig(
                format=u'%(filename)s[LINE:%(lineno)d]# %(levelname)-8s [%(asctime)s] %(message)s',
                level=self.logging_level,
                filename=self.logging_file
                )
        else:
            logging.basicConfig(
                format=u'%(filename)s[LINE:%(lineno)d]# %(levelname)-8s [%(asctime)s] %(message)s',
                level=self.logging_level
            )

        # clear existing logfile (we do not use rotation or archives and so on)
        with open(self.logging_file, 'w'):
            pass

        return True

    def prepare_env_variables(self):
        """ prepare environment variables for GoldenGate ggsci / adminclient """
        ogg_env = os.environ.copy()
        self.logging_prefix = '[' + ogg_env['OGG_HOME'] + '] '
        log_info(self, 'Prepare environment variables for GoldenGate')

        ogg_env['PATH'] = (
            ogg_env['ORACLE_HOME'] + '/bin:' +
            ogg_env['ORACLE_HOME'] + '/lib:' +
            ogg_env['OGG_HOME'] + ':' +
            ogg_env['OGG_HOME'] + '/bin:' +
            os.environ['PATH']
        )

        if self.platform_type in 'Windows':
            log_error(self, 'ERROR: Windows not supported yet. Exit now')
            print('ERROR: Windows not supported yet. Exit now')
            sys.exit(1)
        else:
            self.ogg_cmd_prefix = (
                'ORACLE_HOME=' + ogg_env['ORACLE_HOME'] +
                ' OGG_HOME=' + ogg_env['OGG_HOME']
            )

            if self.platform_type in ('Linux', 'SunOS'):
                ogg_env['LD_LIBRARY_PATH'] = ogg_env['LD_LIBRARY_PATH']+':'+ ogg_env['ORACLE_HOME'] + '/lib:' + ogg_env['OGG_HOME'] + ':' + ogg_env['OGG_HOME'] + '/lib:/usr/lib:/lib'
                self.ogg_cmd_prefix = self.ogg_cmd_prefix + ' LD_LIBRARY_PATH=' + ogg_env['LD_LIBRARY_PATH']
            elif self.platform_type == 'AIX':
                ogg_env['LIBPATH'] = ogg_env['LIBPATH'] + ':' + ogg_env['ORACLE_HOME'] + '/lib:' + ogg_env['OGG_HOME'] + ':' + ogg_env['OGG_HOME'] + '/lib:/usr/lib:/lib'
                self.ogg_cmd_prefix = self.ogg_cmd_prefix + ' LIBPATH=' + ogg_env['LIBPATH']
            else:
                self.ogg_cmd_prefix = ''

        self.env_vars = ogg_env
        return True

    def get_architecture_and_console(self):
        """ detect current OGG installation type and OGG console command """

        if self.platform_type in 'Windows':
            log_error(self, 'Windows platform not supported yet. Exiting')
            print('Windows platform not supported yet. Exiting')
            sys.exit(1)
        else:
            #find console command on UNIX os
            if os.access(self.env_vars['OGG_HOME'] + '/ggsci', os.X_OK):
                #ggsci executable found
                self.ogg_architecture = 'classic'
                self.ogg_console = self.ogg_cmd_prefix + ' ' + self.env_vars['OGG_HOME'] + '/ggsci'
                log_debug(self, 'ggsci found, architecture = ' + self.ogg_architecture)

            elif os.access(self.env_vars['OGG_HOME'] + '/bin/adminclient', os.X_OK):
                #bin/adminclient executable found
                self.ogg_architecture = 'microservices'
                #check if OGG_VAR_HOME variable present for microservices
                if os.getenv('OGG_VAR_HOME') is None:
                    log_error(self, 'Detected ' + self.ogg_architecture + ' environment, but variable OGG_VAR_HOME is not set. Exiting')
                    print('ERROR: detected ' + self.ogg_architecture + ' environment, but variable OGG_VAR_HOME is not set. Exiting')
                    sys.exit(1)
                #add OGG_VAR_HOME to command line parameter
                self.ogg_console = (
                    self.ogg_cmd_prefix +
                    ' OGG_VAR_HOME=' + self.env_vars['OGG_VAR_HOME'] + ' ' +
                    self.env_vars['OGG_HOME'] + '/bin/adminclient'
                )
                log_debug(self, 'adminclient found, architecture = ' + self.ogg_architecture)
            else:
                print('ERROR: Neither ggsci nor adminclient console program found for OGG. Cannot get info. Exiting')
                log_error(self, 'Neither ggsci nor adminclient console program found for OGG. Cannot get info. Exiting')
                sys.exit(1)

            log_debug(self, 'command to run = ' + self.ogg_console)

            if self.ogg_architecture == 'microservices':
                ogg_sm_config_file = ''
                ogg_sm_config_data = ''
                #ogg_sm_port = ''
                #ogg_sm_security_enabled = False
                out = ''
                cmd = 'find ' + self.env_vars['OGG_VAR_HOME'] + ' -type f -name ServiceManager-config.dat | xargs -i ls -atr {} | tail -n 1'
                try:
                    devnull = open(os.devnull, 'wb')
                    out = subprocess.check_output(cmd, shell=True, stderr=devnull)
                    ogg_sm_config_file = out.strip()
                except Exception as e: # pylint: disable=broad-except,invalid-name
                    print('Cannot find last config file for ServiceManager, error was:')
                    print(str(e))
                    sys.exit(1)

                #DEBUG
                #print('ServiceManager config file=' + ogg_sm_config_file)

                if ogg_sm_config_file != '':
                    try:
                        # print('try to read port from json config file')
                        ogg_sm_config_data = json.load(open(ogg_sm_config_file, 'r'))
                        self.sm_port = str(ogg_sm_config_data['config']['network']['serviceListeningPort'])
                        self.sm_security_enabled = ogg_sm_config_data['config']['security']
                    except Exception as e: # pylint: disable=broad-except,invalid-name
                        print('ERROR: cannot load data from ServiceManager config file. Error was:')
                        print(str(e))
                        sys.exit(1)
                else:
                    print('ERROR: cannot find and read Service Manager config file in OGG_VAR_HOME=' + self.env_vars['OGG_VAR_HOME'] + '/.. Exiting')
                    log_error(self, 'ERROR: cannot find Service Manager config file in OGG_VAR_HOME=' + self.env_vars['OGG_VAR_HOME'] + '/.. Exiting')
                    sys.exit(1)

            #DEBUG
            #print ('sm port=' + self.sm_port)
            #print ('sm security enabled=' + str(self.sm_security_enabled))
            #print(os.uname()[1])

            return True

    def aquire_single_run_mutex(self):
        """ aquire a lock for a single script run """
        #get unique name for lock_file, depending on OGG_HOME
        self.lock_file = self.lock_file + self.env_vars['OGG_HOME'].replace('/', '_').replace('\\', '_')
        log_debug(self, 'Trying to lock ' + self.lock_file + ' exclusively')

        try:
            self.mutex = open(self.lock_file, 'w')
            fcntl.flock(self.mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
            log_debug(self, 'Success. Got exclusive lock on ' + self.lock_file)
        except Exception as e: # pylint: disable=broad-except,invalid-name
            print('ERROR: cannot aquire a lock on ' + self.lock_file + ':')
            print(str(e))
            print('1) check the path and permissions 2) another script may still be active (see: ps -ef). Exiting now')
            sys.exit(1)

        #some info logged
        log_info(self, 'Script using OGG_HOME = ' + self.env_vars['OGG_HOME'])
        log_info(self, 'Script using ORACLE_HOME = ' + self.env_vars['ORACLE_HOME'])

        log_debug(self, 'Environment variables:')
        for key in self.env_vars:
            log_debug(self, key + '=' + self.env_vars[key])

        return True

    def prepare_ogg_script(self):
        """ prepare GoldenGate console script """
        cmd = self.ogg_console + ' << EOF\n'

        if self.ogg_architecture == 'microservices':
          #additional connect string here for microservices
            cmd = cmd + 'connect '
            if self.sm_security_enabled:
                cmd = cmd + 'https://'
            else:
                cmd = cmd + 'http://'

            #shame on me, I've used hardcoded user/pwd here)
            cmd = cmd + os.uname()[1] + ':' + self.sm_port + '/ as oggmonitor password TQYt3u5@ !\n' # pylint: disable=no-member

        #common OGG commands
        cmd = (
            cmd +
            'shell printf "\\n==== INFO * SECTION START ====\\n"\n' +
            'info * detail\n' +
            'shell printf "\\n==== INFO * SECTION END ====\\n"\n' +
            'shell printf "\\n==== INFO ALL SECTION START ====\\n"\n' +
            'info all\n' +
            'shell printf "\\n==== INFO ALL SECTION END ====\\n"\n' +
            'shell printf "\\n==== GETLAG SECTION START ====\\n"\n' +
            'send * getlag\n' +
            'shell printf "\\n==== GETLAG SECTION END ====\\n"\n' +
            'shell printf "\\n==== MANAGER SECTION START ===\\n"\n' +
            'info mgr\n' +
            'shell printf "\\n==== MANAGER SECTION END ====\\n"\n' +
            'exit\n' +
            'EOF'
            )
        # DEBUG
        #print('command to execute:')
        self.ogg_script = cmd
        #print(self.ogg_script)

        return True

    def set_unix_timestamp(self):
        """ Get current unix timestamp for zabbix_sender """
        log_info(self, 'Get current unix timestamp for zabbix_sender')
        time.tzset() # pylint: disable=no-member
        utc_string = time.strftime('%s')
        log_debug(self, 'utc_string =' + utc_string)
        self.utc_string = utc_string
        return True


    def get_ogg_script_output(self):
        """ run OGG script in console command and get output """
        log_info(self, 'Run ' + self.ogg_console + ' and get output')
        try:
            # for DEBUG only!
            #print('TEST MODE: reading from /tmp/ogg_output.txt')
            #with open('/tmp/ogg_output.txt' , 'r') as f:
            # output = f.read()
            # next line for production usage
            #output = subprocess.check_output(self.ogg_scriptz shell=True, stderr=subprocess.STDOUT)?
            output = get_shell_output(cmd=self.ogg_script, use_shell=True, use_stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e: # pylint: disable=invalid-name
            log_error(self, 'Unexpected error! Error stack is as follows:')
            log_error(self, e.output)
            print('ERROR: OGG script failed. Error was:')
            print(str(e))
            sys.exit(1)

        self.ogg_script_output = output
        self.ogg_script_outlist = filter(None, self.ogg_script_output.splitlines())

        # DEBUG
        #print(self.ogg_script_output)
        #txtfile = open('/tmp/ogg_output.txt','w')
        #for item in output:
        #	txtfile.write(item)
        #	txtfile.close ()
        #	sys.exit(0)
        return True

    def parse_output_get_static_settings(self):
        """ parse output and get static settings, i.e. version, database """
        log_info(self, 'Parsing GoldenGate output')
        outlist = self.ogg_script_outlist

        log_debug(self, 'Raw GoldenGate output:')
        i = 0
        for line in outlist:
            log_debug(self, str(i) + ': ' + line)
            i += 1

        #get ogg version and database
        log_info(self, 'Get version and database')
        try:
            parse_start = 0
            parse_end = outlist.index('==== INFO * SECTION START ====')

            if parse_end > parse_start:
                for i in range(parse_start, parse_end):
                    splitted_line = outlist[i].split(' ')
                    if ('Oracle GoldenGate Command Interpreter for' in outlist[i]
                            or 'Oracle GoldenGate Administration Client for' in outlist[i]):
                        self.ogg_database = splitted_line[5]
                        log_info(self, 'OGG for database=' + self.ogg_database)
                    if 'Version' in splitted_line[0]:
                        self.ogg_version = splitted_line[1]
                        log_info(self, 'OGG version=' + self.ogg_version)
        except Exception as e: # pylint: disable=broad-except,invalid-name
            log_error(self, 'Error: cannot find version or database information in OGG console output')
            print('Error: cannot find version or database information in OGG console output')
            print(e)
            sys.exit(1)
        return True

    def parse_output_info_section(self):
        """ get processes, statuses, chekpoints, json string from info * and info all sections """
        log_info(self, 'Get processes for JSON discovery, checkpoints and statuses')
        outlist = self.ogg_script_outlist
        ogg_list = self.ogg_zabbix_list
        process_name = ''
        process_type = ''
        process_list = [] # for JSON discovery
        status_name = ''
        status_dict = {}
        checkpoint_dict = {}
        process_dict = self.process_dictionary
        process_trail = ''
        process_trail_type = ''
        process_seq = '0'
        process_rba = '0'
        process_scn = '0'
        process_pid = '0'
        try:
            # discover replicats and extracts information first
            parse_start = outlist.index('==== INFO * SECTION START ====')
            parse_end = outlist.index('==== INFO * SECTION END ====')

            if parse_end > parse_start:
                log_debug(self, 'info * elements:')
                for i in range(parse_start, parse_end):
                    splitted_line = list(filter(None, outlist[i].split(' ')))
                    log_debug(self, str(splitted_line))

                    if splitted_line[0] in ('EXTRACT', 'REPLICAT'):
                        process_name = splitted_line[1]
                        process_type = splitted_line[0]
                        process_list.append(process_name)
                        process_trail = ''
                        process_seq = '0'
                        process_rba = '0'
                        process_scn = '0'
                        status_name = splitted_line[splitted_line.index('Status')+1]
                        if status_name.strip() != '':
                            status_dict[process_name] = status_name.strip()

                        if 'Process ID' in outlist[i]:
                            process_pid = splitted_line[2]
                            # DEBUG
                            #print(*process=' + process_name + ', pid=' + process_pid)
                            #sys.exit(0)

                        if 'Trail Name' in outlist[i]:
                            if process_type == 'EXTRACT':
                                trail_splitted_line = list(filter(None, outlist[i+1].split(' ')))
                                process_trail = trail_splitted_line[0]
                                process_seq = trail_splitted_line[1]
                                process_rba = trail_splitted_line[2]
                                process_trail_type = trail_splitted_line[4]

                        if 'Log Read Checkpoint File' in outlist[i]:
                            if process_type == 'REPLICAT':
                                trail_splitted_line = list(filter(None, outlist[i].split(' ')))
                                process_trail_type = 'LOCALTRAIL'
                                regex = re.search('\d', trail_splitted_line[4][trail_splitted_line[4].rfind('/')+1:len(trail_splitted_line[4])])  # pylint: disable=anomalous-backslash-in-string
                                process_seq = str(int(trail_splitted_line[4][regex.start() + trail_splitted_line[4].rfind('/')+1:len(trail_splitted_line[4])]))
                                process_rba = list(filter(None, outlist[i+1].split(' ')))[3]
                                process_trail = trail_splitted_line[4][0:trail_splitted_line[4].rfind('/')+1 + regex.start()]


                            if ' SCN ' in outlist[i]:
                                process_scn = outlist[i][outlist[i].find('(')+1:outlist[i].find(')')]

                        if 'Checkpoint Lag' in outlist[i]:
                            checkpoint_dict[process_name] = splitted_line[2].replace(',', '').replace('.', '')

                        if process_name != '':
                            process_dict[process_name] = [process_trail, process_trail_type, process_seq, process_rba, process_scn, process_pid]
        except Exception as e: # pylint: disable=broad-except,invalid-name
            log_error(self, 'Error: cannot process "info * " section in the ggsci output')
            print('Error: cannot process "info * " section in the ggsci output')
            print(str(e))
            sys.exit(1)

        # DEBUG
        #for key in process_dict:
        #print(key)
        #print(process_dict[key])
        #sys.exit(0)

        try:
            # add technical processes to process list from info all section
            parse_start = outlist.index('==== INFO ALL SECTION START ====')
            parse_end = outlist.index('==== INFO ALL SECTION END ====')
            if parse_end > parse_start:
                log_debug(self, 'info all elements:')
                for i in range(parse_start, parse_end):
                    splitted_line = list(filter(None, outlist[i].split(' ')))
                    log_debug(self, str(splitted_line))
                    if splitted_line[0] in ('MANAGER', 'ADMINSRV', 'ADMINSRVR', 'DISTSRVR', 'PMSRVR', 'RECVSRVR'):
                        process_name = splitted_line[0]
                        process_list.append(process_name)
                        process_trail = 'NONE'
                        process_seq = '0'
                        process_rba = '0'
                        process_trail_type = 'NONE'
                        process_scn = '0'
                        process_pid = '-1'
                        process_dict[process_name] = [process_trail, process_trail_type, process_seq, process_rba, process_scn, process_pid]
                        if splitted_line[1].strip() != '':
                            status_dict[process_name] = splitted_line[1]
        except Exception as e: # pylint: disable=broad-except,invalid-name
            log_error(self, 'Error: cannot process "info all" section in the ggsci output')
            sys.exit(1)

        for key in status_dict:
            ogg_list.append('ogg.process[' + key + ',status] ' + self.utc_string + ' "' + status_dict[key] + '"')
        #
        ## DEBUG
        ##print(status_dict)
        #
        for key in checkpoint_dict:
            splitted_line = checkpoint_dict[key].split(':')
            # calculate checkpoint lag from HH:MM:SS to seconds
            ogg_list.append(
                'ogg.process[' + key + ',chkptlag] ' + self.utc_string + ' "' +
                str(
                    int(splitted_line[0])*60*60 +
                    int(splitted_line[1])*60 +
                    int(splitted_line[2])
                ) + '"'
            )
        #
        ## DEBUG
        ##print(checkpoint_dict)
        #
        for key in process_dict:
            ogg_list.append('ogg.process[' + key + ',trail_name] ' + self.utc_string + ' "' + process_dict[key][0] + '"')
            ogg_list.append('ogg.process[' + key + ',trail_type] ' + self.utc_string + ' "' + process_dict[key][1] + '"')
            ogg_list.append('ogg.process[' + key + ',seq] ' + self.utc_string + ' "' + process_dict[key][2] + '"')
            ogg_list.append('ogg.process[' + key + ',rba] ' + self.utc_string + ' "' + process_dict[key][3] + '"')
            ogg_list.append('ogg.process[' + key + ',scn] ' + self.utc_string + ' "' + process_dict[key][4] + '"')

        #build JSON string for zabbix discovery
        json_string = self.zbx_discovery_key + ' ' + self.utc_string + ' {"data":[ '
        current_pos = 0
        for proc in process_list:
            current_pos += 1
            json_string += '{"{#OGG_PROCESS}":"' + proc + '"}'
            if current_pos < len(process_list):
                json_string += ', '
        json_string += ']}'

        log_debug(self, 'JSON string:' + json_string)
        log_debug(self, 'OGG list:')
        log_debug(self, str(ogg_list))

        self.json_string = json_string
        self.process_dictionary = process_dict
        #add to list for zabbix sender
        self.ogg_zabbix_list = ogg_list

        return True

    def parse_output_getlag_section(self):
        """ processing GETLAG section and lags for processes """
        log_info(self, 'Get lag for processes')
        outlist = self.ogg_script_outlist
        process_name = ''
        process_lag = ''
        ogg_list = self.ogg_zabbix_list
        try:
            parse_start = outlist.index('==== GETLAG SECTION START ====')
            parse_end = outlist.index('==== GETLAG SECTION END ====')

            if parse_end > parse_start:
                log_debug(self, 'GETLAG output elements:')
                for i in range(parse_start, parse_end):
                    splitted_line = list(filter(None, outlist[i].split(' ')))
                    log_debug(self, str(splitted_line))
                    if 'sending getlag request to' in outlist[i].lower():
                        process_name = splitted_line[5]
                        try:
                            next_splitted_line = list(filter(None, outlist[i+1].split(' ')))
                        except Exception: # pylint: disable=broad-except
                            print('Error: cannot parse GETLAG section. "next_splitted_line" is EOF')
                            sys.exit(1)
                        if 'Last record lag ' in outlist[i+1]:
                            process_lag = next_splitted_line[3].replace(',', '').replace('.', '')
                            ogg_list.append('ogg.process [' + process_name + ',lag] ' + self.utc_string + ' "' + str(process_lag) + '"')
                        elif 'No records yet processed' in outlist[i+1]:
                            process_lag = '0'
                            ogg_list.append('ogg.process[' + process_name + ',lag] ' + self.utc_string + ' "' + str(process_lag) + '"')
                        else:
                            pass # not determined (may be coordinator)
                    elif outlist[i].find('not currently running') > 0:
                        pass # no lag information provides for stopped/abended processes. Only checkpoints available
                    elif 'Average Lag:' in outlist[i]:
                        # we have coordinator here, get this value
                        process_lag = splitted_line[2].replace(',', '').replace('.', '')
                        ogg_list.append('ogg.process[' + process_name + ',lag] ' + self.utc_string + ' "' + str(process_lag) + '"')
            else:
                log_warn(self, 'Empty getlag section in the ggsci output!')
        except Exception as e: # pylint: disable=broad-except,invalid-name
            log_error(self, 'Error: cannot proceed "send * getlag" section in the OGG console output')
            print(str(e))

        # DEBUG
        #for i in ogg_list:
        # print(i)?

        log_debug(self, '==========================')
        log_debug(self, 'Current OGG list')
        log_debug(self, str(ogg_list))
        log_debug(self, '==========================')

        self.ogg_zabbix_list = ogg_list

        return True

    def parse_output_manager_identify(self):
        """ manager port identification for OGG classic """
        port_number = ''
        process_dict = self.process_dictionary
        outlist = self.ogg_script_outlist

        if self.ogg_architecture == 'classic':
            log_info(self, 'Get manager info for classic mode')
        #try:
            parse_start = outlist.index('==== MANAGER SECTION START ====')
            parse_end = outlist.index('==== MANAGER SECTION END ====')

            if parse_end > parse_start:
                log_debug(self, 'MGR: output elements:')
                for i in range(parse_start, parse_end):
                    splitted_line = list(filter(None, outlist[i].split(' ')))
                    log_debug(self, str(splitted_line))

                    if splitted_line:
                        if outlist[i].find('Manager is running') > -1:
                            port_number = splitted_line[5][splitted_line[5].rfind('.')+1:splitted_line[5].rfind(',')]
                            process_pid = re.search('Process ID ([0-9]*)', outlist[i]).group(1)
                            process_dict['MANAGER'] = ['NONE', 'NONE', '0', '0', '0', process_pid]
                            break
            else:
                log_warn(self, 'Empty "info mgr" section!')
            #except Exception as e:
            #log_error(self, 'Error: cannot proceed "info mgr" section in the ggsci output')
            #sys.exit(1)

            if port_number == '':
                print('Error: cannot identify manager port (manager not running?). We need it for zabbix')
                sys.exit(1)

            #get manager identification from config file if needed and set up hostname for zabbix
            if self.use_hostname_for_zabbix == 'NO':
                log_debug(self, ' user_hostname_for_zabbix = YES, now searching for OGG_INSTANCE_ID in mgr.prm')
                # get OGG_INSTANCE_ID from mgr.prm
                if self.platform_type == 'Windows':
                    cmd = 'echo ' + self.short_hostname.upper()
                else:
                    cmd = 'grep "COMMENT OGG_INSTANCE_ID=" ' + self.env_vars['OGG_HOME'] + '/dirprm/mgr.prm | awk -F= \'{print $2}\''
                try:
                    with open(os.devnull, 'w') as devnull:
                        output = get_shell_output(cmd, use_shell=True, use_stderr=devnull)
                        #subprocess.check_output(cmd, shell=True, stderr=devnull)
                        log_debug(self, ' ' + cmd + ' output was: ' + output)

                        if output != '':
                            #found metadata, use it as a zabbix host name
                            log_debug(self, ' found OGG_INSTANCE_ID in mgr.prm. Value=' + output)
                            self.zbx_hostname = 'OGG_' + re.sub('\W+', '', output) + '_' + str(port_number)  # pylint: disable=anomalous-backslash-in-string
                        else:
                            #we didnt find any metadata in manager config. Fallback to hostname
                            log_debug(self, ' Didnt find OGG_INSTANCE_ID in mgr.prm. Fallback to hostname')
                            self.zbx_hostname = 'OGG_' + self.short_hostname.upper() + '_' + str(port_number)
                except Exception as e: # pylint: disable=broad-except,invalid-name
                    log_error(self, 'Unexpected error when parsing mgr.prm! Error stack is as follows:')
                    log_error(self, str(e))
                    sys.exit(1)
            else:
                # "use hostname" detected in ini file
                self.zbx_hostname = 'OGG_' + self.short_hostname.upper() + '_' + str(port_number)
        elif self.ogg_architecture == 'microservices':
            port_number = self.sm_port
            # we dont have mgr.prm in Microservices architecture, so we use hostname anyway
            self.zbx_hostname = 'OGG_' + self.short_hostname.upper() + '_' + str(port_number)

        # DEBUG
        #print('zbx_hostname = ' + self.zbx_hostname)

        return True

    def processes_memory(self):
        """ get ogg processes memory from OS """
        log_info(self, 'Run OS specific memory command and get output')
        pid_string = ''
        process_dict = self.process_dictionary
        for key in process_dict:
            if process_dict[key][5] not in ('-l', '0'): # skip fake process_id
                pid_string = pid_string + ',' + process_dict[key][5]
        pid_string = pid_string[1:len(pid_string)]

        #we do not use anything except ps command, because no shared memory used by OGG processes
        if self.platform_type == 'Linux':
            cmd = 'ps -o pid= -o vsz= -p ' + pid_string +' | column -t | awk \'{bytes=$2*1024; printf "%s %.0f\\n",$1,bytes;}\''
        elif self.platform_type == 'SunOS':
            cmd = 'ps -o pid= -o vsz= -p ' + pid_string + ' | awk \'{bytes=$2*1024; printf "%s %.0f\\n",$1,bytes;}\''
        elif self.platform_type == 'Windows':
            cmd = 'echo 0 0'# not implemeted
        elif self.platform_type == 'AIX':
            cmd = 'ps -o pid= -o vsz= -p ' + pid_string + ' | awk \'{bytes=$2*1024; printf "%s %.0f\\n",$1,bytes;}\''
            #cmd = ('svmon -U ogg -O unit=KB,maxbufsize=20MB | grep -v -E "AUnit|A=|AUser"| awk \'{total=($2+$4)*1024; printf "%
        else:
            cmd = 'echo 0 0'
        try:
            output = get_shell_output(cmd, use_shell=True, use_stderr=subprocess.STDOUT)
            #subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT)
            log_debug(self, 'memory command was: ' + cmd)
            log_debug(self, 'memory output was: ' + output)
        except Exception as e: # pylint: disable=broad-except,invalid-name
            log_error(self, 'Unexpected error when checking processes memory. Error stack is as follows:')
            log_error(self, str(e))
            sys.exit(1)

        outlist = filter(None, output.splitlines())
        process_mem = '0'
        process_pid = '0'
        # temporary list for loop
        process_dict_temp = process_dict.copy()

        for line in outlist:
            splitted_line = line.split(' ')
            process_pid = splitted_line[0]
            process_mem = splitted_line[1]
            # DEBUG
            #print('process pid =' + process_pid)
            #print('process mem =' + process_mem)

            for key in process_dict_temp:
                if process_dict_temp[key][5] == process_pid:
                    #DEBUG
                    #print('add memory = ' + process_mem + ' for process = ' + process_pid + ', process name = ' + key)
                    process_dict[key] = [process_dict[key][0], process_dict[key][1], process_dict[key][2], process_dict[key][3], process_dict[key][4], process_dict[key][5], process_mem]

        # destroy temporary dict
        process_dict_temp.clear()

        for key in process_dict:
            if len(process_dict[key]) == 6:
                # append 0 bytes to processes not found in OS (unusual but possible)
                process_dict[key] = [process_dict[key][0], process_dict[key][1], process_dict[key][2], process_dict[key][3], process_dict[key][4], process_dict[key][5], '0']
            self.ogg_zabbix_list.append('ogg.process[' + key + ',memory] ' + self.utc_string + ' "' + process_dict[key][6] + '"')
            self.ogg_total_memory = self.ogg_total_memory + int(process_dict[key][6])

        self.process_dictionary = process_dict
        # DEBUG
        #print(key)
        #print('mem = ' + process_dict[key][6])
        #print('len = ' + str(len(process_dict[key])))
        log_debug(self, 'total OGG memory = ' + str(self.ogg_total_memory) + ' bytes')
        #print('total OGG memory = ' + str(self.ogg_total_memory) + ' bytes')
        #print(self.process_dictionary)
        return True

    def send_to_zabbix(self):
        """ sending data to zabbix by using zabbix_sender utility """
        log_info(self, 'Prepare data for zabbix')
        #zbx_hostname = *OGG_' + short_hostname.upper() + '_' + str(port_number)
        zbx_data = []
        zbx_data.append(self.zbx_hostname + ' '+ self.json_string)
        for i in self.ogg_zabbix_list:
            zbx_data.append(self.zbx_hostname + ' ' + i)
        zbx_data.append(self.zbx_hostname + ' ogg.environment_id ' + self.utc_string + ' "' + self.environment + '"')
        zbx_data.append(self.zbx_hostname + ' ogg.memory_usage ' + self.utc_string + ' "' + str(self.ogg_total_memory) + '"')
        zbx_data.append(self.zbx_hostname + ' ogg.version ' + self.utc_string + ' "' + self.ogg_version + '"')
        zbx_data.append(self.zbx_hostname + ' ogg.database ' + self.utc_string +' "'+ self.ogg_database + '"')
        zbx_data.append(self.zbx_hostname + ' ogg.platform ' + self.utc_string + ' "' + self.platform_type + '"')
        zbx_data.append(self.zbx_hostname + ' ogg.script_version ' + self.utc_string + ' "' + VERSION + '"')

        log_info(self, 'Data for zabbix_sender:')
        for i in zbx_data:
            log_info(self, i)
            # DEBUG
            #print (i)
        #sys.exit(0)

        log_info(self, 'Run zabbix_sender')
        for zbx_server in self.zabbix_servers.split(','):
            cmd = self.zabbix_sender + ' -w -z ' + zbx_server + ' -T -i -'
            try:
                # we use different shell executor because we interactively sending data to pipe from list
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=True
                )
                exit_output = proc.communicate('\n'.join(zbx_data))
                exit_code = proc.returncode

                if exit_code == 0:
                    log_info(self, 'Zabbix sender succesfully sent data to server: ' + zbx_server)
                    log_info(self, 'Exit code was: ' + str(exit_code))
                    log_info(self, 'STDOUT was: ' + exit_output[0])
                elif exit_code != 0:
                    log_error(self, 'Zabbix sender failed during sending data to server: ' + zbx_server + '!')
                    log_error(self, 'Command was: ' + cmd + ' <metrics in a loop>')
                    log_error(self, 'Exit/Signal was: ' + str(exit_code))
                    log_error(self, 'STDOUT was: ' + exit_output[0])
                    log_error(self, 'STDERR was: ' + exit_output[1])
            except Exception as e: # pylint: disable=broad-except,invalid-name
                log_error(self, 'Error while running zabbix sender! Command was: ' + cmd)
                log_error(self, e)
        return True

    def export_json_for_cmdb(self):
        """ export data for cmdb system in JSON format if set up """
        if self.json_cmdb_file is not None:
            log_info(self, 'Exporting data to json file for cmdb')
            # root subtree
            cmdb_data = {}
            cmdb_data['INSTANCE_NAME'] = self.zbx_hostname
            cmdb_data['ENVIRONMENT'] = self.environment
            cmdb_data['VERSION'] = self.ogg_version
            cmdb_data['DATABASE'] = self.ogg_database
            cmdb_data['PLATFORM'] = self.platform_type
            cmdb_data['HOSTNAME'] = self.short_hostname
            # all processes subtree
            cmdb_data['PROCESSES'] = {}
            for key in self.process_dictionary:
                # particular process subtree
                cmdb_data['PROCESSES'][key] = {}
                # particular process branches
                cmdb_data['PROCESSES'][key]['TRAIL'] = {}
                cmdb_data['PROCESSES'][key]['TRAIL_TYPE'] = {}
                # particular process values
                cmdb_data['PROCESSES'][key]['TRAIL'] = self.process_dictionary[key][0]
                cmdb_data['PROCESSES'][key]['TRAIL_TYPE'] = self.process_dictionary[key][1]

            cmdb_file = self.json_cmdb_file + '.' + self.zbx_hostname
            try:
                with open(cmdb_file, 'w') as fp: # pylint: disable=invalid-name
                    json.dump(cmdb_data, fp)
                    os.chmod(cmdb_file, 0o644)
                log_info(self, 'Data was exported to ' + cmdb_file)
                fp.close()
            except Exception as e: # pylint: disable=broad-except,invalid-name
                log_error(self, 'cannot export json to CMDB file ' + cmdb_file)
                log_error(self, str(e))
                print('WARNING: cannot export json to CMDB file ' + cmdb_file)
            else:
                log_info(self, 'Skipping export JSON file, because no file is given')
                log_info(self, 'You can set up ini file parameter like EXPORT_JSON_FOR_CDMDB=/tmp/file.json if you need this feature')

    def cleanup(self):
        """ cleanup: releasing mutex lock file and delete it """
        log_info(self, 'Releasing lock on ' + self.lock_file)
        try:
            fcntl.flock(self.mutex, fcntl.LOCK_UN)
            self.mutex.close()
            log_debug(self, 'Deleting ' + self.lock_file)
            os.remove(self.lock_file)
        except Exception as e: # pylint: disable=broad-except,invalid-name
            log_error(self, 'Error while releasing lock file ' + self.lock_file)
            log_error(self, str(e))
            sys.exit(1)
        log_info(self, 'Finished at ' + time.strftime("%Y-%m-%d %H:%M:%S"))
        return True

def main():
    """ program starts here """
    if (os.getenv('OGG_HOME') is None) or (os.getenv('ORACLE_HOME') is None):
        print('Environment variables OGG_HOME or ORACLE_HOME are not set. Exiting')
        sys.exit(1)

    # create monitor object, set initial constants and variables
    ogg = OggZabbix()

    # parse command line arguments, set variables for monitor object
    ogg.parse_arguments()

    # read ini file with variables, if required from command line
    if ogg.args.configfile is not None:
        ogg.read_inifile()

    # enable logging and required debugging level
    ogg.log_and_debug()

    log_info(ogg, 'Script starting')
    log_debug(ogg, 'The arguments are: ' + str(sys.argv))
    log_debug(ogg, 'Platform: ' + ogg.platform_type)

    # prepare ENV for OGG console run
    ogg.prepare_env_variables()

    # detect architecture and choose OGG console command
    ogg.get_architecture_and_console()

    # get mutex file for single OGG console run
    log_debug(ogg, 'Aquire a lock for a single instance running')
    ogg.aquire_single_run_mutex()

    # prepare script for running ogg console
    ogg.prepare_ogg_script()

    # get current timestamp for zabbix
    ogg.set_unix_timestamp()

    # run script and get output
    ogg.get_ogg_script_output()

    # parse output and get some static settings: version, database
    ogg.parse_output_get_static_settings()

    # parse output and get processes information
    ogg.parse_output_info_section()
    ogg.parse_output_getlag_section()
    ogg.parse_output_manager_identify()

    # get OGG processes memory from OS
    ogg.processes_memory()

    # DEBUG
    #for attr in dir(ogg):
    # print('ogg.%s = %r' % (attr, getattr(ogg,attr)))

    # send data to zabbix
    ogg.send_to_zabbix()

    # export json file for cdmdb
    ogg.export_json_for_cmdb()

    # finishing
    ogg.cleanup()
    sys.exit(0)

if __name__ == '__main__':
    main()

"""
Changelog:
0.1:
- initial version

0.2:
 - added logging module
 - added -debug key

0.3:
- added -logfile key

0.3.1:
- added logfile rotation

0.3.2:
- added memory usage

0.3.3:
- multiple zabbix servers added delimited with comma

0.3.4:
- added check for Time Since Chkpt
- fixed split() for 'info all' section

0.3.5:
small fixes

0.3.6:
- JSON processes are discovered in send * getlag section (for threads) + info all
- added Solaris (SunOS) and Linux support. Windows was not tested

0.3.7:
- added wrapper for monitoring multiple ogg running. ORACLE_HOME and OGG_HOME discovered from running processes
- added INI file for common settings, arguments parsing changed, with command line priority
- changed logger, added prefix with OGG_HOME, to dictinct OGG logs
- different LOCK files for multiple OGG running, avoid locking from another script

0.3.8
- moved all discovery to info * and info all section
- fixed coordinator lag (use Average lag for it)
- use short hostname for zabbix host_name, not FQDN, e.g. myhost instead of myhost.mydomain.ru
- small fixes

0.3.9
- added trail name, seq, rba, trail_type

0.3.10
- added SCN for extract

0.3.11
- bug fixes

0.3.12
- added memory by process (Virtual Size from ps command)
- unique logfile name for every instance + KEEP_LOG_FILES = 1, primarily for rotation

0.3.13
- added log file usage check. If any other process use it (read or write), when we exiting and not overwrite it

0.3.14
- OGG Microservices added

0.3.15
- bugs fixed with USE_HOSTNAME_FOR_ZABBIX parameter

0.4.1
- refactoring: use classes and function
- removed: KEEP_LOG_FILES and LOGFILE_MAX_SIZE parameters. We use only single log file

0.4.2
- added parameter EXPORT_JSON_FOR_CDMDB for ini file and -j for command line for exporting data to JSON (for cmdb)

TODO:
- rewrite get_shell_output() to use in python 2.6
- rewrite all for python3 support
"""  # pylint: disable=pointless-string-statement
