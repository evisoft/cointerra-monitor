# Standard BSD license, blah blah, with 1 modification below

# Copyright (c) 2014, Erik Anderson  eanders@pobox.com
# All rights reserved.
# https://github.com/dprophet/cointerra-monitor
# TIPS are appreciated.  None of us can afford to have these machines down:
#  BTC: 12VqRL4qPJ6Gs5V35giHZmbgbHfLxZtYyA
#  LTC: LdQMsSAbEwjGZxMYyAqQeqoQr2otPhJ5Gx

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the Organization nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.

# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL Erik Anderson BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# However!!!! If this doesnt catch a failure case of yours please email me the
#    cointerra_monitor.log and cgminer.log files so I can modify it to catch
#    your issues too.
# 
# Additional Python Dependencies (use pip to install):
#      paramiko                  - SSH2 protocol library
#     

# I highly recommend the use of some kinds of miner monitoring agents.  I have yet to see any ASIC/GPU gigs run perfectly.
# Either hardware or software issues ends up shutting down your miner until you realize, OMG the coins stopped!  That
# can be 1-14+ days since you had the last issue.  Complacency kills a miners returns.  Monitoring Agents will keep you
# from always having to check statuses.

import socket
import sys
import traceback
import time
import copy
import logging

import smtplib
import email
import bz2

import json
import os
import urllib2

#SSH and SCP
import paramiko
import scpclient

# For MobileMiner Reporting
import MobileMinerAdapter

#
# Configurations
#

cgminer_port = 4028
cointerra_ssh_user = 'root'
log_name = 'cgminer.log'
cointerra_log_file = '/var/log/' + log_name

#all emails from this script will start with this
email_subject_prefix = 'Cointerra Monitor'

email_warning_subject = 'Warning'  #subject of emails containing warnings (Like temperature)
email_error_subject = 'Error'      #subject of emails for errors (these are serious and require a reboot)

monitor_interval = 30  #interval between checking the cointerra status (in seconds), Ideal if using MobileMiner
monitor_wait_after_email = 60  #waits 60 seconds after the status email was sent
monitor_restart_cointerra_if_sick = True  #should we reboot the cointerra if sick/dead. This should ALWAYS be set to true except development/artificial errors
monitor_send_email_alerts = True  #should emails be sent containing status information, etc.

max_temperature = 80.0  #maximum temperature before a warning is sent in celsius
max_core_temperature = 92.0  #maximum temperature of 1 core before a warning is sent in celsius

n_devices = 0  #Total nunber of ASIC processors onboard.  We will query for the count.
n_max_error_count = 3  # How many errors before you reboot the cointerra
n_reboot_wait_time = 120  #How many seconds after the the reboot of the cointerra before we restart the loop
n_hardware_reboot_percentage = 5  #If the hardware error percentage is over this value we will reboot.  -1 to disable

sLogFilePath = os.getcwd()  # Directory where you want this script to store the Cointerra log files in event of troubles
sMonitorLogFile = sLogFilePath + '/cointerra_monitor.log'

bDebug = False

# Possible logging levels are
#  logging.DEBUG     This is a lot of logs.  You will likely need this level of logging for support issues
#  logging.INFO      Logs confirmations that everything is working as expected.  This should be the default level
#  logging.WARNING   Logs warning.  Issues that did not cause a reboot are logged here.  Like temperature and hash rates.  
#  logging.ERROR     Loss errors.  Script exceptions and issues we discovered with the Cointerra hardware
#  logging.CRITICAL  This script doesnt use this level
nLoggingLevel = logging.DEBUG

#
# Configurations
#

#
# For checking the internet connection
#

def internet_on():
    try:
        response = urllib2.urlopen('http://www.google.com/', timeout = 10)
        return True
    except urllib2.URLError as err: pass
    
    return False

# Class allows communications to the cgminer RPC port on the Cointerra hardware.  Parses return
# string into a Python data structure
class CgminerClient:
    def __init__(self, host, port):
        self.host = host
        self.rpc_port = port
        self.logger = None

    def command(self, command, parameter):
        # sockets are one time use. open one for each command
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        received = {}
        received['message'] = None
        # Set the error status first.  Will clear later.  Reason for this is I have had cgminer crash between sending and receiving of command
        # and I need more debugging to see how python will handle a closed socket read.
        received['error'] = 'Unknown error for command=' + command + ' params=' + str(parameter)

        try:
            mycommand = ""
            if parameter:
                mycommand = json.dumps({"command": command, "parameter": parameter})
            else:
                mycommand = json.dumps({"command": command})

            if self.logger:
                self.logger.debug('host ' + self.host + ' port:' + str(self.rpc_port) + ', command:' + mycommand)
            else:
                print 'host ' + self.host + ' port:' + str(self.rpc_port) + ', command:' + mycommand

            sock.connect((self.host, self.rpc_port))
            self._send(sock, mycommand)
            received['message'] = self._receive(sock)
        except Exception as e:
            received['error'] = 'SOCKET_ERROR(' + self.host + '): ' + str(e)
            print received['error']
            if self.logger:
                self.logger.error(received['error'] + '\n' + traceback.format_exc())
            sock.close()
            return received

        try:
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        except:
            pass # restart makes it fail, but it's ok

        # the null byte makes json decoding unhappy
        try:
            decoded = json.loads(received['message'].replace('\x00', ''))
            myprettyjson = json.dumps(decoded, sort_keys=True, indent=4)

            if self.logger:
                self.logger.debug('Received command(' + command + ') results=' + myprettyjson)
            else:
                print 'Received command(' + command + ') results=' + myprettyjson

            received['message'] = decoded
            received['error'] = None
            return received
        except Exception as e:
            print e
            received['error'] = 'Decoding exception: ' + str(e) + '\n Message(' + str(len(received['message'])) + ') received was:' + received['message']
            print received['error']
            self.logger.error(received['error'] + '\n' + traceback.format_exc())
            return received

    def _send(self, sock, msg):
        totalsent = 0
        while totalsent < len(msg):
            sent = sock.send(msg[totalsent:])
            if sent == 0:
                raise RuntimeError("socket connection broken")
            totalsent = totalsent + sent

    def _receive(self, sock, size=65500):
        msg = ''
        while True:
            chunk = sock.recv(size)
            if chunk == '':
                # end of message
                break
            msg = msg + chunk
        return msg

    def setLogger (self, logger):
        self.logger = logger

    def setCointerraIP (self, sIP):
        self.host = sIP





class JSONMessageProcessor:
    def __init__(self, logger):
        self.logger = logger


    def AscicCountBlock(self, sStatsObject, sAscicCountJSON):
        
        self.logger.debug('Processing ascic count block')

        sStatsObject['asics'] = {}

        sStatsObject['asics']['asic_count'] = sAscicCountJSON['ASCS'][0]['Count']

        return sStatsObject


    def CoinBlock(self, sStatsObject, sCoinJSON):

        self.logger.debug('Processing coin block')

        sStatsObject['coin'] = sCoinJSON['COIN'][0]['Hash Method']

        return sStatsObject


    def PoolBlock(self, sStatsObject, sPoolJSON):
        self.logger.debug('Processing pool block')

        sStatsObject['pools'] = {}
        sStatsObject['pools']['pools_array'] = []

        sStatsObject['pools']['pool_count'] = len(sPoolJSON['POOLS'])

        for iPool in range(sStatsObject['pools']['pool_count']):
            poolurl = sPoolJSON['POOLS'][iPool]['Stratum URL']
            poolstatus = sPoolJSON['POOLS'][iPool]['Status']
            poolAccepted = sPoolJSON['POOLS'][iPool]['Accepted']
            poolRejected = sPoolJSON['POOLS'][iPool]['Rejected']
            poolWorks = sPoolJSON['POOLS'][iPool]['Works']
            poolNumber = sPoolJSON['POOLS'][iPool]['POOL']
            poolDiscarded = sPoolJSON['POOLS'][iPool]['Discarded']
            poolPriority = sPoolJSON['POOLS'][iPool]['Priority']
            poolQuota = sPoolJSON['POOLS'][iPool]['Quota']
            poolWorks = sPoolJSON['POOLS'][iPool]['Works']
            poolGetFailures = sPoolJSON['POOLS'][iPool]['Get Failures']
            iTime = sPoolJSON['POOLS'][iPool]['Last Share Time']
            poolLastShareTime = time.strftime('%m/%d/%Y %H:%M:%S', time.localtime(iTime))

            sStatsObject['pools']['pools_array'].insert(poolNumber, dict([('URL', poolurl), ('status', poolstatus), ('accepted', poolAccepted), \
                                                                          ('rejected', poolRejected), ('works', poolWorks), \
                                                                          ('discarded', poolDiscarded), ('quota', poolQuota), \
                                                                          ('priority', poolPriority), ('works', poolWorks), \
                                                                          ('get_failures', poolGetFailures), ('last_share_time', poolGetFailures), \
                                                                          ('last_share_time', poolLastShareTime)]))

        return sStatsObject



    def StatsBlock(self, sStatsObject, sStatsJSON):
        self.logger.debug('Processing stats block')

        sStatsObject['stats'] = {}
        iLen = len(sStatsJSON['STATS'])
        sStatsObject['stats']['stats_count'] = iLen
        sStatsObject['stats']['stats_array'] = []

        for iStat in range(iLen):
            result = sStatsJSON['STATS'][iStat]
            thisStat = {}
            thisStat['id'] = result['ID']
            thisStat['stat_number'] = result['STATS']

            if result['ID'].startswith('CTA'):
                # this is a ASIC stat
                thisStat['avg_core_temp'] = 0
                thisStat['hw_errors'] = 0
                thisStat['type'] = 'asic'
                thisStat['board_num'] = result['Board number']
                thisStat['calc_hashrate'] = result['Calc hashrate']
                thisStat['ambient_avg'] = float(result['Ambient Avg']) / float(100)
                thisStat['num_asics'] = result['Asics']
                thisStat['board_num'] = result['Board number']
                thisStat['dies'] = result['Dies']
                thisStat['dies_active'] = result['DiesActive']
                thisStat['active'] = result['Active']
                thisStat['inactive'] = result['Inactive']
                thisStat['cores'] = result['Cores']
                thisStat['underruns'] = result['Underruns']
                thisStat['serial'] = result['Serial']
                thisStat['elapsed'] = result['Elapsed']
                thisStat['uptime'] = result['Uptime']
                thisStat['rejected_hashrate'] = result['Rejected hashrate']
                thisStat['total_hashes'] = result['Total hashes']
                thisStat['pump_rpm'] = result['PumpRPM0']
                thisStat['fm_date'] = result['FW Date']
                thisStat['fm_revision'] = result['FW Revision']

                thisStat['core_temps'] = []

                # Calculate the average core temperature and hardware errors
                for iDies in range(thisStat['dies']):
                    sKey = 'CoreTemp' + str(iDies)
                    thisStat['avg_core_temp'] = thisStat['avg_core_temp'] + result[sKey]

                    thisStat['core_temps'].insert(iDies, float(result[sKey]) / 100 )

                    sKey = 'HWErrors' + str(iDies)

                    thisStat['hw_errors'] = thisStat['hw_errors'] + result[sKey]

                thisStat['avg_core_temp'] = float(thisStat['avg_core_temp'] / float(100))
                if thisStat['dies'] != 0:
                    thisStat['avg_core_temp'] = float(thisStat['avg_core_temp'] / float(thisStat['dies']))

                iId = 0
                sKey = 'FanRPM' + str(iId)
                myval = result.get(sKey)

                while myval != None:
                    if thisStat.get('fans') == None:
                        thisStat['fans'] = {}
                        thisStat['fans']['fan_count'] = 0

                    thisStat['fans']['fan_count'] = thisStat['fans']['fan_count'] + 1
                    thisStat['fans'][sKey] = myval

                    iId = iId + 1
                    sKey = 'FanRPM' + str(iId)
                    myval = result.get(sKey)


                # build an array of arrays that can be used to keep track of individual cores inside each asic
                thisStat['asic_status'] = {}
                thisStat['asic_status']['id'] = thisStat['id']
                thisStat['asic_status']['dies'] = thisStat['dies']
                thisStat['asic_status']['dies_active'] = thisStat['dies_active']

                thisStat['asic_status']['pipebitmaphex'] = []
                thisStat['asic_status']['alive'] = []
                for iAsicNum in range(thisStat['num_asics']):
                    iCoreNum = 0

                    sKey = 'Asic' + str(iAsicNum) + 'Core' + str(iCoreNum)
                    bHasKey = result.has_key(sKey)
                    oCores = []
                    oAlive = []

                    while bHasKey == True:
                        sVal = result[sKey]
                        oCores.append(sVal)
                        if sVal == '00000000000000000000000000000000':
                            oAlive.append(False)
                        else:
                            oAlive.append(True)

                        iCoreNum = iCoreNum + 1
                        sKey = 'Asic' + str(iAsicNum) + 'Core' + str(iCoreNum)
                        bHasKey = result.has_key(sKey)

                    # Append the cores array to the asic array
                    thisStat['asic_status']['pipebitmaphex'].append(oCores)
                    thisStat['asic_status']['alive'].append(oAlive)


            else:
                thisStat['type'] = 'pool'
                thisStat['bytes_recv'] = result['Bytes Recv']
                thisStat['bytes_recv'] = result['Bytes Sent']
                thisStat['work_difficulty'] = result['Work Diff']


            sStatsObject['stats']['stats_array'].insert(iStat, thisStat)



        return sStatsObject

    # Process the ASIC RPC message
    def AscicBlock(self, sStatsObject, nAsicNumber, sAscicJSON):
        self.logger.debug('Processing ascic block')

        if sStatsObject['asics'].get('asics_array') == None:
            sStatsObject['asics']['asics_array'] = []

        result = sAscicJSON['ASC'][0]

        asicStatus = result['Status']  #If this is ever bad, not good!!!
        asicName = result['Name']
        asicHash5s = result['MHS 5s']
        asicHashAvg = result['MHS av']
        asicHardwareErrors = result['Hardware Errors']
        asicRejected = result['Rejected']
        asicAccepted = result['Accepted']
        asicID = result['ID']
        asicEnabled = result['Enabled']
        asicDeviceRejectPercent = result['Device Rejected%']
        asicLastShareTime = time.strftime('%m/%d/%Y %H:%M:%S', time.localtime(result['Last Share Time']))
        asicLastValidWork = time.strftime('%m/%d/%Y %H:%M:%S', time.localtime(result['Last Valid Work']))

        sStatsObject['asics']['asics_array'].insert(nAsicNumber, dict([('status', asicStatus), ('name', asicName), ('hash5s', asicHash5s), \
                                                                       ('hashavg', asicHashAvg), ('hw_errors', asicHardwareErrors), \
                                                                       ('rejected', asicRejected), ('id', asicID), ('enabled', asicEnabled), \
                                                                       ('accepted', asicAccepted), ('reject_percent', asicDeviceRejectPercent), \
                                                                       ('last_share_t', asicLastShareTime), ('last_valid_t', asicLastValidWork)]))
        
        return sStatsObject

    # Processes the summary JSON return from a summary command
    def SummaryBlock(self, sStatsObject, sSummaryJSON):
        self.logger.debug('Processing summary block')

        result = sSummaryJSON['SUMMARY'][0]

        sStatsObject['summary'] = {}
        sStatsObject['summary']['hw_errors'] = result['Hardware Errors']
        sStatsObject['summary']['hash5s'] = result['MHS 5s']
        sStatsObject['summary']['hashavg'] = result['MHS av']
        sStatsObject['summary']['pool_reject_percent'] = result['Pool Rejected%']
        sStatsObject['summary']['pool_stale_percent'] = result['Pool Stale%']
        sStatsObject['summary']['blocks_found'] = result['Found Blocks']    # Lucky?  Should have solo mined
        sStatsObject['summary']['discarded'] = result['Discarded']
        sStatsObject['summary']['rejected'] = result['Rejected']
        sStatsObject['summary']['get_failures'] = result['Get Failures']
        sStatsObject['summary']['get_works'] = result['Getworks']

        return sStatsObject



class CointerraSSH:
    def __init__(self, host, port, user, passwd, sLogFilePath, logger):
        self.host = host
        self.ssh_port = port
        self.user = user
        self.password = passwd
        self.sLogFilePath = sLogFilePath
        self.logger = logger

    def setHost(self, sHost):
        self.host = sHost

    def setPassword(self, sPassword):
        self.password = sPassword

    # Creates an SSH connection
    def createSSHClient(self):
        self.logger.debug('createSSHClient host ' + self.host + ' port:' + str(self.ssh_port))

        try:
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(self.host, self.ssh_port, self.user, self.password)
            return client
        except Exception as e:
            print e
            self.logger.error('Error in createSSHClient. =' + str(e) + '\n' + traceback.format_exc())
            return None

    # Creates an SCP file transfer client
    def CreateScpClient(self):
        ssh_client = self.createSSHClient()
        scp = scpclient.SCPClient(ssh_client)

    def reboot(self):

        try:
            self.logger.error('Rebooting the cointerra ' + self.host)
            print 'Rebooting the cointerra ' + self.host
            ssh_client = self.createSSHClient()
            transport = ssh_client.get_transport()
            session = transport.open_session()
            session.exec_command('/sbin/reboot')
            if session.recv_ready():
                data = session.recv(4096)
                print 'Reboot results =' + data

            time.sleep(5)
            ssh_client.close()
            print 'Cointerra ' + + self.host + ' has been rebooted'
            self.logger.error('Cointerra ' + self.host + ' has been rebooted')

        except Exception as e:
            print e
            self.logger.error('Error in reboot. =' + str(e) + '\n' + traceback.format_exc())
            if ssh_client:
                ssh_client.close()



    def ReturnCommandOutput(self, sCommand):

        sData = ""

        try:
            self.logger.info('Executing command ' + sCommand + ' on cointerra ' + self.host)
            ssh_client = self.createSSHClient()
            transport = ssh_client.get_transport()
            session = transport.open_session()
            session.exec_command(sCommand)
            time.sleep(1)

            # Read everything on the socket
            while session.recv_ready():
                sData += session.recv(1024)

            ssh_client.close()


        except Exception as e:
            print e
            self.logger.error('Error in command "' + sCommand + '" =' + str(e) + '\n' + traceback.format_exc())
            if ssh_client:
                ssh_client.close()

        # Convert string to utf-8 because some non-ascii char
        return unicode(sData, "utf-8")



    # Executes a ps command on the cointerra looking for the cgminer program
    def isCGMinerRunning(self):
        bReturn = False

        try:
            self.logger.info('running isCGMinerRunning')
            ssh_client = self.createSSHClient()

            transport = ssh_client.get_transport()
            session = transport.open_session()
            session.exec_command('ps -deaf | grep cgminer')
            time.sleep(1)
            if session.recv_ready():
                data = session.recv(4096)

                nIndex = data.find('/opt/cgminer')

                if bDebug:
                    print 'received over SSH =' + data
                    print 'Index for /opt/cgwatcher =' + str(nIndex)

                if nIndex > 0:
                    bReturn = True
            else:
                self.logger.warning('This should not happen. session.recv_ready() isnt ready')

            ssh_client.close()

        except Exception as e:
            print 'Error thrown in isCGMinerRunning ='
            print e
            self.logger.error('Error in isCGMinerRunning. =' + str(e) + '\n' + traceback.format_exc())
            if ssh_client:
                ssh_client.close()

        return bReturn

    def ScpLogFile(self, sFileName):

        bReturn = False

        try:

            self.logger.info('SCP file:' + sFileName + ' from host ' + self.host)

            ssh_client = self.createSSHClient()
            transport = ssh_client.get_transport()

            myscpclient = scpclient.SCPClient(transport)

            if bDebug:
                print 'SCP file' + sFileName + ' to host:' + self.host

            #this will copy the file from the cointerra to the local PC
            myscpclient.get(sFileName, self.sLogFilePath)

            # sFileName is of the remote TerraMiner.  Parse it to get the filename without the path
            spath, sname = os.path.split(sFileName)

            if os.path.isfile(self.sLogFilePath + "/" + sname) == True:
                self.compressFile(self.sLogFilePath + "/" + sname, True)
                bReturn = True
            else:
                self.logger.error('Failed to SCP ' + sFileName + ' from host ' + self.host)

        except Exception as e:
            print 'Error thrown in ScpLogFile ='
            print e
            self.logger.error('Error in ScpLogFile. =' + str(e) + '\n' + traceback.format_exc())
            if ssh_client:
                ssh_client.close()

        return bReturn

    def compressFile (self, sUncompressedFilename, bDeleteOriginalFile):
        #compress the log file.  Can be very large for emailing
        try:

            oComp = bz2.BZ2Compressor()
            oSource = file(sUncompressedFilename, 'rb')
            oDest = file(sUncompressedFilename + '.bz2', 'wb')
            sBlock = oSource.read( 2048 )
            while sBlock:
                cBlock = oComp.compress( sBlock )
                oDest.write(cBlock)
                sBlock = oSource.read( 2048 )
            cBlock = oComp.flush()
            oDest.write( cBlock )
            oSource.close()
            oDest.close()

            if bDeleteOriginalFile == True:
                os.remove(sUncompressedFilename)

        except Exception as e:
            print 'Error thrown in compressFile ='
            print e
            self.logger.error('Error in compressFile. =' + str(e) + '\n' + traceback.format_exc())




#
# Utils
#

def SendEmail(sMachineName, from_addr, to_addr_list, cc_addr_list,
              subject, message, login, password,
              smtpserver,
              file_logger,
              sCGMinerLogfile = None,
              sMonitorLogfile = None):

    if (sCGMinerLogfile == None) and (sMonitorLogfile == None):
        header = 'From: %s\n' % from_addr
        header += 'To: %s\n' % ','.join(to_addr_list)
        header += 'Cc: %s\n' % ','.join(cc_addr_list)
        header += 'Subject: %s\n\n' % (email_subject_prefix + '_' + sMachineName + ': ' + subject)

        try:
            server = smtplib.SMTP(smtpserver)
            server.starttls()
            server.login(login, password)
            server.sendmail(from_addr, to_addr_list, header + message)
            server.quit()
        except Exception as e:
            print "Error sending email for machine=" + sMachineName + '\n' + str(e) + '\n' + traceback.format_exc()
            file_logger.error("Error sending email for machine=" + sMachineName + '\n' + str(e) + '\n' + traceback.format_exc())

    else:

        msg = email.MIMEMultipart.MIMEMultipart()
        msg['Subject'] = email_subject_prefix + '_' + sMachineName + ': ' + subject
        msg['From'] = from_addr
        msg['To'] = ', '.join(to_addr_list)

        msg.attach(email.MIMEText.MIMEText(message))

        if sCGMinerLogfile:

            # SCP sometimes fails to copy a log file.  Check before trying to attach
            if os.path.isfile(sCGMinerLogfile) == True:
                part = email.MIMEBase.MIMEBase('application', "octet-stream")
                part.set_payload(open(sCGMinerLogfile, "rb").read())
                email.Encoders.encode_base64(part)

                part.add_header('Content-Disposition', 'attachment; filename="cgminer.log.bz2"')

                msg.attach(part)

        if sMonitorLogfile:

            # Dont check for log file existence,  We should crash if no file so we can see the error
            part = email.MIMEBase.MIMEBase('application', "octet-stream")
            part.set_payload(open(sMonitorLogfile, "rb").read())
            email.Encoders.encode_base64(part)

            part.add_header('Content-Disposition', 'attachment; filename="cointerra_monitor.log.bz2"')

            msg.attach(part)

        try:
            server = smtplib.SMTP(smtpserver)
            server.starttls()
            server.login(login, password)
            server.sendmail(from_addr, to_addr_list, msg.as_string())
            server.quit()
        except Exception as e:
            print "Error sending email for machine=" + sMachineName + '\n' + str(e) + '\n' + traceback.format_exc()
            file_logger.error("Error sending email for machine=" + sMachineName + '\n' + str(e) + '\n' + traceback.format_exc())


# Utility to compares the current asic statuses vs the initial statups statuses stored in oInitialAsicStatuses
# Return of False means a reboot is necessary
# oStat = oStatsStructure['stats']['stats_array'][iCount]
def compareAcisStatuses(sMachineName, oInitialAsicStatuses, oAsicStat, logger):
    bReturn = True

    oArray = oInitialAsicStatuses[sMachineName]['asic_status']

    for iCounter in range(len(oArray)):
        if oArray[iCounter]['id'] == oAsicStat['id']:
            # This is the right element
            iLenInitialAsics = len(oAsicStat['asic_status']['alive'])
            iLenCurrentAsics = len(oArray[iCounter]['alive'])

            if iLenInitialAsics == iLenCurrentAsics:

                for iCounter2 in range(iLenInitialAsics):
                    iLenInitialCores = len(oAsicStat['asic_status']['alive'][iCounter2])
                    iLenCurrentCores = len(oArray[iCounter]['alive'][iCounter2])

                    if iLenInitialCores == iLenCurrentCores:
                        for iCounter3 in range(iLenInitialCores):
                            # Is the core disabled and the core status not equal to the status when monitor initially started
                            if oAsicStat['asic_status']['alive'][iCounter2][iCounter3] == False and \
                                    oAsicStat['asic_status']['alive'][iCounter2][iCounter3] != oArray[iCounter]['alive'][iCounter2][iCounter3]:
                                bReturn = False
                                logger.error('Machine:' + sMachineName + ', Asic:' + oAsicStat['id'] + ', chip:[' + str(iCounter2) + \
                                    '][' + str(iCounter3) + '] went offline' )
                                print 'Machine:', sMachineName, ', Asic:', oAsicStat['id'], ', chip:[', str(iCounter2), \
                                    '][', str(iCounter3), '] went offline'
                    else:
                        bReturn = False
                        logger.error('Machine:' + sMachineName + ' initial core count(' + str(iLenInitialCores) + \
                                     ') and current core count(' + str(iLenCurrentCores) + ' is not the same')
                        print 'Machine:' + sMachineName + ' initial core count(' + str(iLenInitialCores) + \
                                     ') and current core count(' + str(iLenCurrentCores) + ' is not the same'

            else:
                bReturn = False
                logger.error('Machine:' + sMachineName + ' it appears an asic went out' )
                print 'Machine:' + sMachineName + ' it appears an asic went out'

    return bReturn



# This is the main execution module
def StartMonitor(client, configs):
    os.system('clear')

    #time internet was lost and reconnected
    internet_lost = 0
    internet_reconnected = 0
    bError = False
    global n_devices
    global n_ambient_temperature
    global n_hardware_reboot_percentage
    global n_max_error_count

    # Delete the old log file
    if os.path.isfile(sMonitorLogFile) == True:
        os.remove(sMonitorLogFile)

    logger = logging.getLogger('CointerraMonitor')
    hdlr = logging.FileHandler(sMonitorLogFile)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    hdlr.setFormatter(formatter)
    logger.addHandler(hdlr) 
    logger.setLevel(nLoggingLevel)
    client.setLogger(logger)
    oInitialAsicStatuses = {}  # This data structure will be populated with asic+die statuses for the first run after startup.
    oInitialAsicStatuses['logged'] = False

    logger.error('Starting cointerra-watcher ' + time.strftime('%m/%d/%Y %H:%M:%S'))

    messageProcessor = JSONMessageProcessor(logger)

    nCointerraCoint = len(configs['machines'])

    sJsonContents = []
    sLastGoodJSONEntry = []
    nErrorCounterArray = []
    nMobileMinerCommandIDs = []
    for iCount in range(nCointerraCoint):
        sJsonContents.append('')
        sLastGoodJSONEntry.append('')
        nErrorCounterArray.append(0)

    oMobileReporter = None
    oSSH = None

    '''
    Build the mobileStructure to look something like this.  Makes posting of stats easier
    {
        "1234-5678-9010": {
            "machines": [
                "CointerraName1",
                "CointerraName2"
            ],
            "mobileminer_email": "my@email.com",
            "remote_commands": [
                true,
                false
            ]
        }
    }
    '''

    mobileStructure = {}
    for iCointerraNum in range(len(configs['machines'])):
        oCurrentMachine = configs['machines'][iCointerraNum]
        sMachineName = oCurrentMachine['machine_name']
        nLen = len(oCurrentMachine['mobileminer'])

        if nLen > 0:
            for iCounter in range(nLen):
                oMobile = oCurrentMachine['mobileminer'][iCounter]
                sApiKey = oMobile['mobileminer_api_key']
                sApiEmail = oMobile['mobileminer_email']
                bRemoteCommands = False
                if oMobile.has_key('remote_commands'):
                    bRemoteCommands = oMobile['remote_commands']

                sValue = None
                if mobileStructure.has_key(sApiKey):
                    sValue = mobileStructure[sApiKey]

                if sValue == None:
                    mobileStructure[sApiKey] = {}
                    mobileStructure[sApiKey]['mobileminer_email'] = sApiEmail
                    mobileStructure[sApiKey]['machines'] = []
                    mobileStructure[sApiKey]['machines'].append(sMachineName)
                    mobileStructure[sApiKey]['remote_commands'] = []
                    mobileStructure[sApiKey]['remote_commands'].append(bRemoteCommands)
                else:
                    sValue['machines'].append(sMachineName)
                    sValue['remote_commands'].append(bRemoteCommands)


    logger.info('mobileStructure=' + json.dumps(mobileStructure, sort_keys=True, indent=4))
    print 'mobileStructure=', json.dumps(mobileStructure, sort_keys=True, indent=4)
    
    while(1):
        logger.info('Start of loop.  Time=' + time.strftime('%m/%d/%Y %H:%M:%S'))

        bWasAMachineRebooted = False

        for iCointerraNum in range(len(configs['machines'])):
            oCurrentMachine = configs['machines'][iCointerraNum]

            output = ''
            bError = False
            bWarning = False
            bSocketError = False
            oStatsStructure = {}

            nMobileMinerCount = 0

            # Get settings from the config JSON file for this loop
            cgminer_host = oCurrentMachine['cointerra_ip_address']
            cointerra_ssh_pass = oCurrentMachine['root_password']
            email_smtp_server = oCurrentMachine['email_smtp_server']
            email_login = oCurrentMachine['email_login']
            email_password = oCurrentMachine['email_password']
            email_from = oCurrentMachine['email_from']
            email_to = oCurrentMachine['email_to']
            sMachineName = oCurrentMachine['machine_name']
            if 'mobileminer' in oCurrentMachine:
                nMobileMinerCount = len(oCurrentMachine['mobileminer'])

            if nMobileMinerCount > 0 and oMobileReporter == None:
                oMobileReporter = MobileMinerAdapter.MobileMinerAdapter(logger, mobileStructure)

            logger.info('Checking machine ' + cgminer_host + '(' + sMachineName + '). Time=' + time.strftime('%m/%d/%Y %H:%M:%S'))

            if oSSH == None:
                oSSH = CointerraSSH(cgminer_host, 22, cointerra_ssh_user, cointerra_ssh_pass, sLogFilePath, logger)

            # Set the host and password for this cointerra
            oSSH.setHost(cgminer_host)
            oSSH.setPassword(cointerra_ssh_pass)

            sMessage = ''

            client.setCointerraIP(cgminer_host)

            oStatsStructure['time'] = time.strftime('%m/%d/%Y %H:%M:%S')
            oStatsStructure['machine_name'] = sMachineName
            oStatsStructure['host'] = cgminer_host

            # get the count of the number of ASIC units in the cointerra
            result = client.command('asccount', None)
            if result['message']:
                messageProcessor.AscicCountBlock(oStatsStructure,result['message'])
                n_devices = oStatsStructure['asics']['asic_count']

                for loop in range(n_devices):
                    result = client.command('asc', str(loop))
                    if result:
                        messageProcessor.AscicBlock(oStatsStructure, loop, result['message'])

            else:
                output = output + '\n\n' + result['error']
                bSocketError = True


            result = client.command('coin', None)
            if result['message']:
                messageProcessor.CoinBlock(oStatsStructure,result['message'])
            else:
                output = output + '\n\n' + result['error']
                bSocketError = True

            result = client.command('pools', None)
            if result['message']:
                messageProcessor.PoolBlock(oStatsStructure,result['message'])
            else:
                output = output + '\n\n' + result['error']
                bSocketError = True
     
            result = client.command('summary', None)
            if result['message']:
                messageProcessor.SummaryBlock(oStatsStructure, result['message'])
            else:
                output = output + '\n\n' + result['error']
                bSocketError = True

            result = client.command('stats', None)
            if result['message']:
                messageProcessor.StatsBlock(oStatsStructure, result['message'])
            else:
                output = output + '\n\n' + result['error']
                bSocketError = True

            #  We dont do anything with this command.  Just want it printed to the log file
            result = client.command('devs', None)

            # Make it more human readable
            sPrettyJSON = json.dumps(oStatsStructure, sort_keys=False, indent=4)

            # print 'new oStatsStructure = ' + sPrettyJSON

            logger.debug('new oStatsStructure = ' + sPrettyJSON)

            if bSocketError == False:

                # Check if this is the first run.  If so copy the ASIC statuses into the
                if oInitialAsicStatuses.has_key(sMachineName) == False:
                    oInitialAsicStatuses[sMachineName] = {}
                    oInitialAsicStatuses[sMachineName]['asic_status'] = []

                    for iCount in range(oStatsStructure['stats']['stats_count']):
                        oStat = oStatsStructure['stats']['stats_array'][iCount]
                        if oStat['type'] == 'asic':
                            oInitialAsicStatuses[sMachineName]['asic_status'].append(copy.deepcopy(oStat['asic_status']))

                # No socket error.  Report to MobileMiner first
                if oMobileReporter != None:
                    oMobileReporter.addDevices(oStatsStructure)

                # The oStatsStructure contains all of the cointerra stats from calls to the cgminer RPC port
                sMessage = sMessage + '#ASIC:' + str(oStatsStructure['asics']['asic_count'])
                for iCount in range(oStatsStructure['asics']['asic_count']):
                    oAsic = oStatsStructure['asics']['asics_array'][iCount]
                    sMessage = sMessage + ' ID:' + str(oAsic['id']) + ':' + oAsic['status'] + '/' + oAsic['enabled']
                    if oAsic['status'] != 'Alive':
                        nErrorCounterArray[iCointerraNum] = nErrorCounterArray[iCointerraNum] + 1
                        output = output + '\n Asic #' + str(iCount) + ' bad status =' + oAsic['status']
                        bError = True
                        break
                    elif oAsic['reject_percent'] > n_hardware_reboot_percentage:
                        nErrorCounterArray[iCointerraNum] = nErrorCounterArray[iCointerraNum] + 1
                        output = output + '\n Asic #' + str(iCount) + ' Hardware Errors too high ' + str(oAsic['reject_percent'])
                        bError = True
                        break
                    elif oAsic['enabled'] != 'Y':
                        nErrorCounterArray[iCointerraNum] = nErrorCounterArray[iCointerraNum] + 1
                        output = output + '\n Asic #' + str(iCount) + ' enabled= ' + oAsic['enabled']
                        bError = True
                        break

                for iCount in range(oStatsStructure['stats']['stats_count']):
                    oStat = oStatsStructure['stats']['stats_array'][iCount]

                    if oStat['type'] == 'asic':
                        sMessage = sMessage + ' ID:' + oStat['id'] + ' DIES:' + str(oStat['dies_active']) + '/' + str(oStat['dies'])
                        if oStat['avg_core_temp'] >= max_temperature or oStat['ambient_avg'] >= max_temperature:
                            bWarning = True
                            output = output + '\n ASIC ID=' + oStat['id'] + ' has a high temperature. avg_core_temp=' + str(oStat['avg_core_temp']) + \
                                ' ambient_avg=' + str(oStat['ambient_avg'])
                        elif oStat['dies'] == 0 or oStat['dies'] != oStat['dies_active'] or oStat['dies'] != 8:

                            # Compare the current ASIC core statuses vs the initial values read when script started
                            bOk = compareAcisStatuses(sMachineName, oInitialAsicStatuses, oStat, logger)

                            if bOk == False:
                                nErrorCounterArray[iCointerraNum] = nErrorCounterArray[iCointerraNum] + 1
                                output = output + '\n' + oStat['id'] + ' has ' + str(oStat['dies_active']) + ' dies but only ' + \
                                    str(oStat['dies']) + ' are active'
                                bError = True
                                break

                        for iCore in range(len(oStat['core_temps'])):
                            if oStat['core_temps'][iCore] >= max_core_temperature:
                                bWarning = True
                                output = output + '\n' + oStat['id'] + ' core#' + str(iCore) + ' has a high temperature of ' + \
                                    str(oStat['core_temps'][iCore]) + '. Max temp is ' + str(max_core_temperature)

                #sMessage = sMessage + ' hashavg:' + str(oStatsStructure['summary']['hashavg'] / 1000000)
                sMessage = sMessage + ' hashavg:{:.3f}T'.format(oStatsStructure['summary']['hashavg'] / 1000000)


            else:
                nErrorCounterArray[iCointerraNum] = nErrorCounterArray[iCointerraNum] + 1

            if (bError == True) or (bSocketError == True):
                if nErrorCounterArray[iCointerraNum] > n_max_error_count:
                    sJsonContents[iCointerraNum] = ''

                    # If a socket error use the last known good JSON contents
                    if bSocketError == True:
                        sJsonContents[iCointerraNum] = sLastGoodJSONEntry[iCointerraNum]
                    else:
                        sJsonContents[iCointerraNum] = sPrettyJSON

                    print oStatsStructure['time'] + output
                    print 'Rebooting machine and sending email.  Will sleep for ' + str(n_reboot_wait_time) + ' seconds'
                    print sJsonContents[iCointerraNum]

                    if oMobileReporter != None:
                        if nMobileMinerCount > 0:
                            for iMobileMinerCounter in range(nMobileMinerCount):
                                oMobileReporter.SendMessage('Fubar!  Rebooting ' + sMachineName, \
                                                            oCurrentMachine['mobileminer'][iMobileMinerCounter]['mobileminer_email'], \
                                                            oCurrentMachine['mobileminer'][iMobileMinerCounter]['mobileminer_api_key'])


                    logger.error('Rebooting machine ' + sMachineName + ' and sending email, error:' + str(nErrorCounterArray[iCointerraNum]) + \
                                 ' of:' + str(n_max_error_count)  + ' Will sleep for ' + str(n_reboot_wait_time) + ' seconds')
                    if len(sJsonContents[iCointerraNum]) > 0:
                        logger.debug(sJsonContents[iCointerraNum])

                    sDMesg = oSSH.ReturnCommandOutput('/bin/dmesg')
                    sLsusb = oSSH.ReturnCommandOutput('/usr/bin/lsusb')

                    logger.debug('dmesg on machine ' + sMachineName + '\n' + sDMesg)
                    logger.debug('lsusb on machine ' + sMachineName + '\n' + sLsusb)

                    oSSH.ScpLogFile(cointerra_log_file)

                    if monitor_restart_cointerra_if_sick == True:
                        oSSH.reboot()
                        bWasAMachineRebooted = True

                    # compress the log file to make smaller before we email it
                    oSSH.compressFile(sMonitorLogFile, False)

                    if monitor_send_email_alerts:
                        SendEmail(sMachineName, from_addr = email_from, to_addr_list = [email_to], cc_addr_list = [],
                                  subject = email_error_subject,
                                  message = output + '\n' + sJsonContents[iCointerraNum],
                                  login = email_login,
                                  password = email_password,
                                  smtpserver = email_smtp_server,
                                  file_logger = logger,
                                  sCGMinerLogfile = sLogFilePath + '/' + log_name + '.bz2',
                                  sMonitorLogfile = sMonitorLogFile + '.bz2')

                    if os.path.isfile(sLogFilePath + '/' + log_name + '.bz2') == True:
                        os.remove(sLogFilePath + '/' + log_name + '.bz2')

                    nErrorCounterArray[iCointerraNum] = 0  # Reset the error counter
                else:
                    logger.warning('Got an error. Counter:' + str(nErrorCounterArray[iCointerraNum]) + ' of:' + str(n_max_error_count) + '\n' + output)
                    print oStatsStructure['time'] + ' ' + sMachineName + ': Got an error. Counter:' + str(nErrorCounterArray[iCointerraNum]) + ' of:' + \
                        str(n_max_error_count) + '\n' + output

            elif bWarning == True:

                sJsonContents[iCointerraNum] = sPrettyJSON

                print oStatsStructure['time'] + ' ' + output
                print 'System warning '
                print sJsonContents[iCointerraNum]

                logger.warning('System warning: ' + output)
                logger.warning(sJsonContents[iCointerraNum])

                oSSH.ScpLogFile(cointerra_log_file)
                oSSH.compressFile(sMonitorLogFile, False)

                if monitor_send_email_alerts:
                    SendEmail(sMachineName, from_addr = email_from, to_addr_list = [email_to], cc_addr_list = [],
                              subject = email_warning_subject,
                              message = output + '\n' + sJsonContents[iCointerraNum],
                              login = email_login,
                              password = email_password,
                              smtpserver = email_smtp_server,
                              file_logger = logger,
                              sCGMinerLogfile = sLogFilePath + '/' + log_name + '.bz2',
                              sMonitorLogfile = sMonitorLogFile + '.bz2')
            else:
                nErrorCounterArray[iCointerraNum] = 0
                print time.strftime('%H:%M:%S') + ' ' + sMachineName.ljust(20) + ' ' + sMessage + '.'
                logger.info(time.strftime('%m/%d/%Y %H:%M:%S') + ' ' + sMachineName.ljust(20) + ' ' + sMessage + '. alive and well')
                sLastGoodJSONEntry[iCointerraNum] = copy.deepcopy(sPrettyJSON)


        # Send the data and clear the array in the mobileminer array
        if oMobileReporter != None:

            # Send all machine stats to all configured multiminers
            oMobileReporter.SendStats()

            # Clear the machine data from the MobileMiner reporter.  Will repopulate next loop through
            oMobileReporter.ClearData()

            # This large block of code processes commands from your MobileMiner.  Currently I only support RESTART
            # which will reboot your cointerra.  START and STOP are not supported as I dont see good mappings between
            # the cgminer RPC command list and the MobileMiner START/STOP

            iCmdLen = 0
            oMachineCommands = oMobileReporter.GetCommands()

            for sMachineName in oMachineCommands:
                oCommands = oMachineCommands[sMachineName]['commands']
                sMobileMinerEmail = oMachineCommands[sMachineName]['mobileminer_email']
                sMobileMinerAppKey = oMachineCommands[sMachineName]['mobileminer_api_key']
                iCmdLen = len(oCommands)

                for iCmdCount in range(iCmdLen):
                    nCmdID = oCommands[iCmdCount]['Id']
                    sCmdString = oCommands[iCmdCount]['CommandText']

                    if nCmdID in nMobileMinerCommandIDs:
                        logger.debug('CmdString=' + sCmdString + ', CommandID(' + str(nCmdID) + \
                                     ') already in array.  Dont double process')
                    else:
                        logger.debug('Received new command:' + sCmdString + ' from ' + \
                                     sMobileMinerEmail + \
                                     ', CommandID(' + str(nCmdID) + '), Machine=' + sMachineName)
                        print 'Received new command:' + sCmdString + ' from ' + \
                                     sMobileMinerEmail + \
                                     ', CommandID(' + str(nCmdID) + '), Machine=' + sMachineName

                        # Safety array so we make sure we dont double process commands.
                        # MobileMiner website sometimes times out and may not delete the command
                        nMobileMinerCommandIDs.append(nCmdID)

                        if sCmdString == 'RESTART':
                            print 'Received a RESTART. Rebooting ' + sMachineName

                            sDMesg = oSSH.ReturnCommandOutput('/bin/dmesg')
                            sLsusb = oSSH.ReturnCommandOutput('/usr/bin/lsusb')

                            logger.debug('dmesg on machine ' + sMachineName + '\n' + sDMesg)
                            logger.debug('lsusb on machine ' + sMachineName + '\n' + sLsusb)

                            oSSH.ScpLogFile(cointerra_log_file)

                            oSSH.reboot()
                            bWasAMachineRebooted = True   # Will cause us to sleep for a while

                            # compress the log file to make smaller before we email it
                            oSSH.compressFile(sMonitorLogFile, False)

                            if monitor_send_email_alerts:
                                SendEmail(sMachineName, from_addr = email_from, to_addr_list = [email_to], cc_addr_list = [],
                                          subject = 'Machine ' + sMachineName + ', was remotely rebooted by ' + sMobileMinerEmail,
                                          message = 'Machine ' + sMachineName + ', was remotely rebooted by ' + sMobileMinerEmail,
                                          login = email_login,
                                          password = email_password,
                                          smtpserver = email_smtp_server,
                                          file_logger = logger,
                                          sCGMinerLogfile = sLogFilePath + '/' + log_name + '.bz2',
                                          sMonitorLogfile = sMonitorLogFile + '.bz2')

                            if os.path.isfile(sLogFilePath + '/' + log_name + '.bz2') == True:
                                os.remove(sLogFilePath + '/' + log_name + '.bz2')

                        elif sCmdString == 'STOP':
                            print 'This is a STOP command.  We dont support STOP commands'
                        elif sCmdString == 'START':
                            print 'This is a START command.  We dont support START commands'

                    # Delete the command from the mobileminer website
                    oMobileReporter.DeleteCommand(nCmdID, sMobileMinerEmail, sMobileMinerAppKey, sMachineName)

        if oInitialAsicStatuses['logged'] == False:
            logger.debug('oInitialAsicStatuses=' + json.dumps(oInitialAsicStatuses, sort_keys=True, indent=4))
            oInitialAsicStatuses['logged'] = True

        if bWasAMachineRebooted == True: 
            time.sleep(n_reboot_wait_time)
        else:
            # Sleep by increments of 1 second to catch the keyboard interrupt
            for i in range(monitor_interval):
                time.sleep(1)

        if bDebug:
            os.system('clear')

    return

if __name__ == "__main__":

    iArgLen = len(sys.argv)
    sHost = sys.argv[1] if iArgLen > 1 else None
    command = sys.argv[2] if iArgLen > 2 else None
    parameter = sys.argv[3] if iArgLen > 3 else None

    configJson = ''
    decodedConfig = None
    myfile = None

    try:
        myfile = open ("config.json", "r")
        configJson=myfile.read()
        decodedConfig = json.loads(configJson)
    except Exception as e:
        print 'ERROR with your config.json file.  Fix it before trying to continue'
        print e
        print 'Traceback =' + traceback.format_exc()
        sys.exit(0)

    # Create the CgminerClient client.  This will let us communicate with the cgminer running on the cointerra box
    client = CgminerClient(decodedConfig['machines'][0]['cointerra_ip_address'], cgminer_port)

    #print 'config.json=', configJson, '\n'

    if command:
        # An argument was specified, ask cgminer and exit
        client.setCointerraIP(sHost)
        result = client.command(command, parameter)
        print str(result) if result else 'Cannot get valid response from cgminer'
        sys.exit(1)
    else:
        # No argument, start the monitor and the http server
        try:

            #start the monitor
            StartMonitor(client, decodedConfig)

        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            # Its important to crash/shutdown here until all bugs are gone.
            print 'Error thrown in main execution path ='
            print e
            print 'Traceback =' + traceback.format_exc()
            client.logger.error('Error thrown in mail execution path =' + str(e) + '\n' + traceback.format_exc())
            sys.exit(0)

