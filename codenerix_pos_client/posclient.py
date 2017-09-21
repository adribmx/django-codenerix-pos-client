#!/usr/bin/env python3
# encoding: utf-8

import os
import json
import uuid
import time

try:
    from subprocess import getstatusoutput
except Exception:
    from commands import getstatusoutput

from ws4py.client.threadedclient import WebSocketClient
from ws4py.exc import HandshakeError

from lib.debugger import Debugger
from lib.cryptography import AESCipher
from lib.timeout import timeout, TimedOutException

from __init__ import __version_name__

from manager import Manager
from webserver import WebServer
import config

from hardware import POSWeight, POSTicketPrinter, POSCashDrawer, POSDNIe, HardwareError


class POSClient(WebSocketClient, Debugger):

    CONNECT_TIMEOUT = 5
    AVAILABLE_HARDWARE = {
        'WEIGHT': POSWeight,
        'TICKET': POSTicketPrinter,
        'CASH': POSCashDrawer,
        'DNIE': POSDNIe,
    }

    manager = Manager()

    def __init__(self, *args, **kwargs):
        # Set debugger
        self.set_debug()
        self.set_name('POSClient')
        self.debug("Starting {}".format(__version_name__), color='blue')

        # Initialize environment
        self.challenge = None
        self.hardware = {}
        self.crypto = AESCipher()
        self.uuid = uuid.UUID(config.UUID)
        self.uuidhex = self.uuid.hex
        self.__encrypt = False
        self.__fully_configured = False

        # Get on which commit are we working
        if os.path.exists("commit.dat"):
            commit = open("commit.dat", "r").read().split("\n")[0]
        else:
            commit = None

        # Find real commit
        # cmd = "git show --format='%H' --no-patch"   # Long HASH
        cmd = "git show --format='%h' --no-patch"   # Short HASH
        status, output = getstatusoutput(cmd)
        if status:
            realcommit = None
        else:
            realcommit = output

        # Build answer
        if commit == realcommit:
            answer = commit
        else:
            if commit and realcommit:
                answer = "{}:{}".format(commit, realcommit)
            elif commit:
                answer = "{}:NOREAL".format(commit)
            elif realcommit:
                answer = "NODATA:{}".format(realcommit)
            else:
                answer = "NODATA:NOREAL"
        # Set commit version
        self.__commit = answer

        # Keep going with warm up
        super(POSClient, self).__init__(*args, **kwargs)

    @property
    def encrypt(self):
        return self.__encrypt

    def shutdown(self):
        if self.manager.isrunning:
            self.manager.shutdown()

    def opened(self):
        self.debug("Connection opened", color="blue")
        self.configure()

    def configure(self):
        self.debug("Requesting config", color="blue")
        self.send({'action': 'get_config'}, None)

    def closed(self, code, reason=None):
        self.debug("Websocket closed", color="blue")

    def send_error(self, msg, ref=None, uid=None):
        self.error("{} (ref:{})".format(msg, ref))
        msg = {'action': 'error', 'error': msg}
        if uid:
            msg['uuid'] = uid.hex
        if self.encrypt:
            self.send(msg, ref)
        else:
            super(POSClient, self).send(json.dumps({'message': msg}))

    def send(self, request, ref):
        # Encode request
        msg = json.dumps({'request': request, 'ref': ref})

        # Build query
        query = {
            'uuid': self.uuidhex,
            'message': self.crypto.encrypt(msg, config.KEY).decode('utf-8'),
        }

        # Encode to JSON
        data = json.dumps(query)

        # Send to remote
        super(POSClient, self).send(data)

    def received_message(self, package):
        # self.debug("New message arrived: {}".format(package), color='yellow')

        try:
            request = json.loads(package.data.decode('utf-8'))
        except Exception:
            request = None

        # Check if we got msg
        if request is not None and isinstance(request, dict):
            message = request.get('message', None)
            if message is not None:

                # Decrypt message
                try:
                    msg = self.crypto.decrypt(message, config.KEY)
                    self.__encrypt = True
                except Exception:
                    self.warning("Message is not encrypted or we have the wrong KEY")
                    msg = message
                try:
                    query = json.loads(msg)
                except Exception:
                    query = None

                if query is not None and isinstance(query, dict):
                    request = query.get('request', None)
                    if request is not None:
                        ref = query.get('ref')
                        if isinstance(request, dict):
                            self.debug("Receive: {}".format(request), color='cyan')
                            self.recv(request, ref)
                        else:
                            self.send_error("Message is not a Dictionary", ref)
                    else:
                        self.error("Message doesn't belong to CODENERIX POS")
                else:
                    if request is None:
                        self.send_error("Message is not JSON or is None")
                    else:
                        self.send_error("Message is not a Dictionary")
            else:
                self.send_error("Missing 'message' or is None")
        else:
            if request is None:
                self.send_error("Request is not JSON or is None")
            else:
                self.send_error("Request is not a Dictionary")

    def recv(self, message, ref):
        action = message.get('action', None)

        if action == 'config':
            if self.manager.isrunning:
                self.debug("Reconfiguration process: Shutting down Manager", color='cyan')
                self.manager.shutdown()

            # Initialize manager
            self.debug("Starting up manager", color='blue')
            self.manager.attach(WebServer(uuid.uuid4(), 'Local Webserver', self.__commit))

            # Get commit ID
            commit = message.get('commit', None)
            if commit:
                # Set new commit
                self.debug("Setting COMMIT to: {}".format(commit), color="cyan")
                with open("commit.dat", "w") as F:
                    F.write(commit)

                # Pull changes
                cmd = "git show --format='%H' --no-patch"   # Long HASH
                status, output = getstatusoutput(cmd)
                if status:
                    lastcommit = output
                else:
                    lastcommit = None

                if commit != lastcommit:
                    status, output = getstatusoutput("git fetch origin")
                    if status:
                        self.error("Couldn't fetch changes from REPOSITORY, I won't try any MERGE!")
                    else:
                        if commit == 'LATEST':
                            extraarg = ''
                        else:
                            extraarg = ' '+commit
                        status, output = getstatusoutput("git merge{}".format(extraarg))
                        if status:
                            self.error("Couldn't merge changes!")

            else:
                # Delete commit.dat
                self.warning("This client is not linked to GITHUB")
                if os.path.exists("commit.dat"):
                    os.unlink("commit.dat")

            # Configure hardware
            self.debug("Setting configuration", color='blue')

            error = False
            for hw in message.get('hardware', []):
                # Get details
                uuidtxt = hw.get('uuid', None)
                kind = hw.get('kind', '')
                config = hw.get('config', {})

                if uuidtxt is not None:
                    uid = uuid.UUID(uuidtxt)
                    if not self.manager.exists_worker(uid):
                        self.debug("    > Configuring ", color='yellow', tail=False)
                        self.debug(str(uid), color='purple', head=False, tail=False)
                        self.debug(" as ", color='yellow', head=False, tail=False)
                        if kind in self.AVAILABLE_HARDWARE:
                            self.debug(kind, color='white', head=False)
                            try:
                                self.manager.attach(self.AVAILABLE_HARDWARE.get(kind)(uid, config))
                            except HardwareError as e:
                                self.send_error("Device {} as {} is wrong configured: {}".format(uid, kind, e), ref, uid)
                                error = True
                        else:
                            self.debug("{}??? - Not setting it up!".format(kind), color='red', head=False)
                else:
                    self.error("    > I found a hardware configuration without UUID, I will not set it up!")

            # Make sure all tasks in manager are running
            self.manager.run(self)

            # If some error during startup
            if error:
                self.error("I have detected some error, I will try to reconfigure system when next message arrives!")
            else:
                # No error happened, we are ready to go
                self.debug("Everything is set up and ready to work", color='green')
                self.__fully_configured = True

        elif action == 'reset':
            self.warning("Got Reset request from Server")
            self.close(reason='Reset requested from Server')
        elif action == 'msg':
            msg = message.get('message', None)
            uid = message.get('uuid', None)
            if msg and uid:
                error = self.manager.recv(msg, ref, uid)
                if error:
                    self.send_error(error, ref)
            elif msg:
                self.send_error("No destination added to your message", ref)
            elif uuid:
                self.send_error("Got message for '{}' with no content", ref)
            else:
                self.send_error("Missing message and destination for your message", ref)
        elif action == 'error':
            self.error("Got an error from server: {}".format(message.get('error', 'No error')))
        elif action == 'ping':
            subref = message.get('ref', '-')
            self.debug("Sending PONG {} (ref:{})".format(subref, ref))
            self.send({'action': 'pong'}, ref)
        elif action != 'config':
            self.send_error("Unknown action '{}'".format(action), ref)

        if action != 'config' and not self.__fully_configured:
            self.debug("Reconfigure system", color='yellow')
            self.send({'action': 'get_config'}, ref)


def watchdog_config(ws):
    # Sleep 10 seconds
    time.sleep(10)
    # Check if we already got configuration
    if not ws.manager.isrunning:
        # We didn't get configuration yet, request configuration again
        ws.configure()
        # Relaunch this function
        watchdog_config(ws)


if __name__ == '__main__':
    keepworking = True
    DEBUG = getattr(config, 'DEBUG', False)
    while keepworking:
        connected = False
        ws = POSClient("ws://{}/codenerix_pos/?session_key={}".format(config.SERVER, uuid.uuid4().hex), protocols=['http-only', 'chat'])
        if DEBUG:
            print()
            print(" /------------------\\")
            print(" | DEBUG is ACTIVE! |  < < < < < <")
            print(" \\------------------/")
            print()
        try:
            timeout(ws.connect, ws.CONNECT_TIMEOUT)
            connected = True
        except TimedOutException:
            ws.error("Connection timed out after {} seconds, I will try to connect again!".format(ws.CONNECT_TIMEOUT))
        except ConnectionRefusedError:
            ws.error("Connection refused, I will try to connect again!")
        except ConnectionResetError:
            ws.error("Connection reset, I will try to connect again!")
        except HandshakeError:
            ws.error("Hasdshake error, I will try to connect again!")
        except Exception as e:
            if DEBUG:
                raise
            else:
                try:
                    ws.error("Uncontrolled ERROR detected, I will try to connect again. Error was: {}".format(e))
                except Exception:
                    ws.error("Uncontrolled ERROR detected, but I can not print the exception, I will try to execute connection process again!".format(e))

        if connected:
            # Start watchdog to start looking for configuration
            watchdog_config(ws)
            # Wait forever
            try:
                ws.run_forever()
            except KeyboardInterrupt:
                keepworking = False
                ws.debug("")
                ws.debug("User requested to exit", color='yellow')
                ws.debug("")
            finally:
                try:
                    ws.shutdown()
                except Exception:
                    pass
                try:
                    ws.close()
                except Exception:
                    pass

        if keepworking:
            ws.warning("Detected disconnection from server: reconnecting WebSocket in 5 seconds!")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                keepworking = False
                ws.debug("")
                ws.debug("User requested to exit", color='yellow')
                ws.debug("")
