# -*- py-indent-offset: 4; coding: iso-8859-1; mode: python -*-
#
# Copyright (C) 2004, 2005, 2006 Mekensleep
#
# Mekensleep
# 24 rue vieille du temple
# 75004 Paris
#       licensing@mekensleep.com
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301, USA.
#
# Authors:
#  Loic Dachary <loic@gnu.org>
#  Henry Precheur <henry@precheur.org> (2004)
#

import time
from string import split, lower
from re import match

from twisted.internet import reactor, defer

from pokereval import PokerEval
from pokerengine.pokergame import PokerGameClient, PokerPlayer
from pokerengine.pokercards import PokerCards
from pokerengine.pokerchips import PokerChips

from pokerengine.pokerengineconfig import Config
from pokernetwork.client import UGAMEClientProtocol, UGAMEClientFactory
from pokernetwork.pokerchildren import PokerChildren, PokerChildBrowser
from pokernetwork.pokerpackets import *
from pokernetwork import upgrade

DEFAULT_PLAYER_USER_DATA = { 'delay': 0, 'timeout': None }

class PokerNetworkGameClient(PokerGameClient):
    def __init__(self, url, dirs):
        PokerGameClient.__init__(self, url, dirs) # is_directing == False
        self.level_skin = ""        

class PokerSkin:
    """Poker Skin"""

    def __init__(self, *args, **kwargs):
        self.settings = kwargs['settings']
        ( self.url, self.outfit ) = self.interpret("random", "random")

    def destroy(self):
        pass

    def interfaceReady(self, interface, display):
        pass

    def interpret(self, url, outfit):
        return (url, outfit)
    
    def getUrl(self):
        return self.url

    def setUrl(self, url):
        self.url = url

    def getOutfit(self):
        return self.outfit

    def setOutfit(self, outfit):
        self.outfit = outfit

    def hideOutfitEditor(self):
        pass

    def showOutfitEditor(self, select_callback):
        pass

#
# Set a flag when an error is logged
#
from twisted.python import log

log.error_occurred = False
log_err = log.err

def err(*args, **kwargs):
    global log_err
    log_err(*args, **kwargs)
    log.error_occurred = True

log.err = err
log.deferr = err

class PokerClientFactory(UGAMEClientFactory):
    "client factory"

    def __init__(self, *args, **kwargs):
        UGAMEClientFactory.__init__(self, *args, **kwargs)
        self.settings = kwargs["settings"]
        self.config = kwargs.get("config", None)
        #
        # Make sure the attributes exists, should an exception occur before
        # it is initialized with an instance of PokerChildren and such. This is done
        # so that the caller does not have to check the existence of the
        # attribute when catching an exception.
        #
        self.interface = None
        self.display = None
        self.children = None

        settings = self.settings
        self.ping_delay = settings.headerGetInt("/settings/@ping")
        self.no_display_packets = settings.headerGet("/settings/@no_display_packets")
        self.name = settings.headerGet("/settings/name")
        self.password = settings.headerGet("/settings/passwd")
        if self.config:
            chips_values = self.config.headerGet("/sequence/chips")
            if not chips_values:
                raise UserWarning, "PokerClientFactory: no /sequence/chips found in %s" % self.config.path
            self.chips_values = map(int, chips_values.split())
        else:
            self.chips_values = [1]
        self.host = "unknown"
        self.port = 0
        self.remember = settings.headerGet("/settings/remember") == "yes"
        self.chat_config = settings.headerGetProperties("/settings/chat")
        if self.chat_config:
            self.chat_config = self.chat_config[0]
            for (key, value) in self.chat_config.iteritems():
                self.chat_config[key] = int(value)
        else:
            self.chat_config = {}
        self.dirs = split(self.settings.headerGet("/settings/path"))
        self.verbose = self.settings.headerGetInt("/settings/@verbose")
        self.delays = self.settings.headerGetProperties("/settings/delays")
        if self.delays:
            self.delays = self.delays[0]
            for (key, value) in self.delays.iteritems():
                self.delays[key] = float(value)
            if self.delays.has_key("round"):
                self.delays["end_round"] = self.delays["round"]
                self.delays["begin_round"] = self.delays["round"]
                del self.delays["round"]
            if not self.delays.has_key("blind_ante_position"):
                self.delays["blind_ante_position"] = self.delays["position"]
        else:
            self.delays = {}
        if self.verbose > 2: print "PokerClient: delays %s" % self.delays
        self.delays_enable = self.settings.headerGet("/settings/@delays") == "true"
        self.skin = PokerSkin(settings = self.settings)
        self.protocol = PokerClientProtocol
        self.games = {}
        self.file2name = {}
        self.first_time = self.settings.headerGet("/settings/name") == "username"
        self.played_time = self.settings.headerGet("/settings/played_time")

        self.children = PokerChildren(self.config, self.settings)
        self.upgrader = upgrade.Upgrader(self.config, self.settings)
        self.upgrader.registerHandler(upgrade.TICK, self.upgradeTick)
        self.upgrader.registerHandler(upgrade.CLIENT_VERSION_OK, self.clientVersionOk)
        self.upgrader.registerHandler(upgrade.NEED_UPGRADE, self.needUpgrade)
        self.upgrader.registerHandler(upgrade.UPGRADE_READY, self.upgradeReady)
        self.upgrader.registerHandler(upgrade.FAILED, self.failedUpgrade)
        self.interface = None

    def __del__(self):
        if hasattr(self, "games"):
            del self.games

    def upgradeTick(self, ratio, message):
        self.display.tickProgressBar(ratio, message)

    def restart(self):
        self.children.killall()
        reactor.disconnectAll()
        self.display.underware.Uninit()
        import sys
        import os
        if os.name != "posix" :
            os.execv("pok3d.exe", ["pok3d.exe", "--restart"])
        else:
            argv = [ sys.executable ]
            argv.extend(sys.argv)
            os.execv(sys.executable, argv)

    def quit(self):
        #
        # !!! The order MATTERS here !!! the renderer must be notified last
        # otherwise leak detection won't be happy. Inverting the two
        # is not fatal and the data will be freed eventually. However,
        # debugging is made much harder because leak detection can't
        # check as much as it could.
        #        
        self.skin.destroy()
        packet = PacketQuit()
        self.display.render(packet)

    def getSkin(self):
        return self.skin
    
    def getUrl(self):
        return self.skin.getUrl()

    def setUrl(self,url):
        return self.skin.setUrl(url)

    def getOutfit(self):
        return self.skin.getOutfit()
    
    def setOutfit(self,outfit):
        return self.skin.setOutfit(outfit)
    
    def translateFile2Name(self, file):
        if not self.file2name.has_key(file):
            config = Config(self.dirs)
            config.load("poker.%s.xml" % file)
            name = config.headerGet("/bet/description")
            if not name:
                name = config.headerGet("/poker/variant/@name")
                if not name:
                    print "*CRITICAL* can't find readable name for %s" % file
                    name = file
            self.file2name[file] = name
        return self.file2name[file]

    def saveAuthToFile(self, name, password, remember):
        settings = self.settings
        self.name = name
        self.password = password
        self.remember = remember
        if remember:
            remember = "yes"
        else:
            remember = "no"
            name = ""
            password = ""
        settings.headerSet("/settings/remember", remember)
        settings.headerSet("/settings/name", name)
        settings.headerSet("/settings/passwd", password)
        settings.save()
        settings.headerSet("/settings/name", self.name)
        settings.headerSet("/settings/passwd", self.password)

    def isOutbound(self, packet):
        return ( packet.type == PACKET_ERROR or
                 packet.type == PACKET_POKER_HAND_LIST or
                 packet.type == PACKET_POKER_PLAYER_INFO or
                 packet.type == PACKET_POKER_USER_INFO or
                 packet.type == PACKET_POKER_HAND_HISTORY or
                 packet.type == PACKET_POKER_PLAYERS_LIST or
                 packet.type == PACKET_POKER_TOURNEY_PLAYERS_LIST or
                 packet.type == PACKET_POKER_TOURNEY_UNREGISTER or
                 packet.type == PACKET_POKER_TOURNEY_REGISTER )

    def isAlwaysHandled(self, packet):
        return ( packet.type == PACKET_POKER_PLAYER_CHIPS or
                 packet.type == PACKET_POKER_CHAT )
    
    def isConnectionLess(self, packet):
        return ( packet.type == PACKET_PROTOCOL_ERROR or
                 packet.type == PACKET_QUIT )

    def getGame(self, game_id):
        if not hasattr(self, "games") or not self.games.has_key(game_id):
            return False
        else:
            return self.games[game_id]

    def getGameByNameNoCase(self, name):
        for (serial, game) in self.games.iteritems():
            if lower(game.name) == name:
                return game
        return None
    
    def getOrCreateGame(self, game_id):
        if not self.games.has_key(game_id):
            game = PokerNetworkGameClient("poker.%s.xml", self.dirs)
            game.verbose = self.verbose
            game.id = game_id
            self.games[game_id] = game

        return self.games[game_id]

    def getGameIds(self):
        return self.games.keys()
    
    def deleteGame(self, game_id):
        del self.games[game_id]

    def packet2game(self, packet):
        if not self.isOutbound(packet) and hasattr(packet, "game_id") and self.games.has_key(packet.game_id):
            return self.games[packet.game_id]
        else:
            return False

    def gameExists(self, game_id):
        return self.games.has_key(game_id)

    def browseWeb(self, path):
        PokerChildBrowser(self.config, self.settings, path)

    def checkClientVersion(self, version):
        self.upgrader.checkClientVersion(version)

    def clientVersionOk(self):
        pass

    def failedUpgrade(self, logs, reason):
        pass

    def needUpgrade(self, version):
        pass

    def upgrade(self, version, excludes):
        self.display.showProgressBar()
        self.upgrader.getUpgrade(version, excludes)

    def upgradeReady(self, target_dir, upgrades_dir):
        self.children.killall()
        reactor.disconnectAll()
        if hasattr(self.display, "underware"):
            self.display.underware.Uninit()
        import sys
        import os
        if os.name != "posix" :
            os.execv(upgrades_dir + "/upgrade.exe", [ upgrades_dir + "/upgrade.exe", '"' + target_dir + '"', '"' + sys.executable + '"' ])
        else:
            os.execv("/bin/sh", [ upgrades_dir + "/upgrade", '-x', upgrades_dir + "/upgrade", upgrades_dir, sys.executable ] + sys.argv)

SERIAL_IN_POSITION = 0
POSITION_OBSOLETE = 1

ABSOLUTE_LAGMAX = 120
DEFAULT_LAGMAX = 15

class PokerClientProtocol(UGAMEClientProtocol):
    """Poker client"""

    def __init__(self):
        UGAMEClientProtocol.__init__(self)
        self.callbacks = {
            'current': {},
            'not_current': {},
            'outbound': {}
            }
        self.setCurrentGameId(None)
        self.pending_auth_request = False
        self.position_info = {}
        self.publish_packets = []
        self.input_packets = []
        self.publish_timer = None
        self.publish_time = 0
        self.publishPackets()
        self.lag = DEFAULT_LAGMAX
        self.lagmax_callbacks = []

    def error(self, string):
        self.message("ERROR " + string)
        
    def message(self, string):
        print self._prefix + string
        
    def setCurrentGameId(self, game_id):
        if hasattr(self.factory, 'verbose') and self.factory.verbose > 2: self.message("setCurrentGameId(%s)" % game_id)
        self.hold(0)
        self.currentGameId = game_id

    def getCurrentGameId(self):
        return self.currentGameId
        
    def connectionMade(self):
        "connectionMade"
        if self.factory.delays_enable:
            self._lagmax = ABSOLUTE_LAGMAX
            self.lag = self.factory.delays.get("lag", DEFAULT_LAGMAX)
        self.no_display_packets = self.factory.no_display_packets
        UGAMEClientProtocol.connectionMade(self)

    def packetDeferred(self, what, name):
        d = defer.Deferred()
        def fire(client, packet):
            d.callback((client, packet))
        self.registerHandler(what, name, fire)
        def unregister(arg):
            self.unregisterHandler(what, name, fire)
            return  arg
        d.addCallback(unregister)
        return d
        
    def registerHandler(self, what, name, meth):
        if name:
            names = [ name ]
        else:
            names = PacketNames.keys()
        if what != True:
            whats = [ what ]
        else:
            whats = [ 'current', 'not_current', 'outbound' ]
        for what in whats:
            callbacks = self.callbacks[what]
            for name in names:
                callbacks.setdefault(name, []).append(meth)
        
    def unregisterHandler(self, what, name, meth):
        if name:
            names = [ name ]
        else:
            names = PacketNames.keys()
        if what != True:
            whats = [ what ]
        else:
            whats = [ 'current', 'not_current', 'outbound' ]
        for what in whats:
            callbacks = self.callbacks[what]
            for name in names:
                callbacks[name].remove(meth)
        
    def normalizeChips(self, game, chips):
        if game.unit in self.factory.chips_values:
            values = self.factory.chips_values[self.factory.chips_values.index(game.unit):]
        else:
            values = []
        list = PokerChips(values, chips).tolist()
        if self.factory.verbose > 4:
            print "normalizeChips: " + str(list) + " " + str(values)
        return list
            
    def updatePlayerChips(self, game, player):
        packet = PacketPokerPlayerChips(game_id = game.id,
                                        serial = player.serial,
                                        bet = player.bet,
                                        money = player.money)
        return packet

    def updatePotsChips(self, game, side_pots):
        packets = []
        
        if not side_pots:
            packet = PacketPokerChipsPotReset(game_id = game.id)
            return [ packet ]
        
        index = 0
        for (amount, total) in side_pots['pots']:
            chips = amount
            bet = self.normalizeChips(game, chips)
            pot = PacketPokerPotChips(game_id = game.id,
                                      index = index,
                                      bet = bet)
            packets.append(pot)
            index += 1
        return packets

    def chipsPlayer2Bet(self, game, player, chips):
        packets = []
        packet = PacketPokerChipsPlayer2Bet(game_id = game.id,
                                            serial = player.serial,
                                            chips = self.normalizeChips(game, chips))
        packets.append(packet)
        packets.append(self.updatePlayerChips(game, player))
        return packets

    def chipsBet2Pot(self, game, player, bet, pot_index):
        packets = []
        if ( pot_index == 0 and
             player.dead > 0 and
             game.isSecondRound() ):
            #
            # The ante or the dead are already in the pot
            #
            bet -= player.dead
        packet = PacketPokerChipsBet2Pot(game_id = game.id,
                                         serial = player.serial,
                                         chips = self.normalizeChips(game, bet),
                                         pot = pot_index)
        packets.append(packet)
        packets.append(self.updatePlayerChips(game, player))
        return packets
        
    def chipsPot2Player(self, game, player, bet, pot_index, reason):
        packet = PacketPokerChipsPot2Player(game_id = game.id,
                                            serial = player.serial,
                                            chips = self.normalizeChips(game, bet),
                                            pot = pot_index,
                                            reason = reason)
        return packet
        
    def handleUserInfo(self, packet):
        print "handleUserInfo: " + str(packet)
        self.user_info = packet

    def handlePersonalInfo(self, packet):
        self.handleUserInfo(packet)
        self.personal_info = packet

    def handleSerial(self, packet):
        self.user.serial = packet.serial
        self.sendPacket(PacketPokerGetUserInfo(serial = packet.serial))

    def handlePlayerInfo(self, packet):
        skin = self.factory.getSkin()
        #
        # Check that the implementation of the outfit is still valid. If it
        # needs upgrade, send it back to the server.
        #
        ( url, outfit ) = skin.interpret(packet.url, packet.outfit)
        if url != packet.url or outfit != packet.outfit:
            ( url_check, outfit_check ) = self.factory.getSkin().interpret(url, outfit)
            #
            # Make sure that we wont loop indefinitely because of an instability of the interpret
            # function. In normal operation the url and outfit returned by interpret must be
            # returned as is when fed to interpret again. If the implementation of interpret
            # fails to implement this stability, don't enter a loop because sending PokerPlayerInfo
            # will return us a PokerPlayerInfo for confirmation of the success.
            #
            if url_check != url or outfit_check != outfit:
                print "*CRITICAL*: PACKET_POKER_PLAYER_INFO: may enter loop packet.url = %s\n url = %s\n url_check = %s\npacket.outfit = %s\n outfit = %s\n outfit_check = %s" % ( packet.url, url, url_check, packet.outfit, outfit, outfit_check )
            else:
                packet.url = url
                packet.outfit = outfit
                self.sendPacket(packet)
        skin.setUrl(url)
        skin.setOutfit(outfit)

    def logout(self):
        self.sendPacket(PacketLogout())
        self.user.logout()

    def gameEvent(self, game_id, type, *args):
        if self.factory.verbose > 4:
            self.message("gameEvent: game_id = %d, type = %s, args = %s" % ( game_id, type, str(args) ))

        forward_packets = self.forward_packets
        if not forward_packets:
            if self.factory.verbose > 3:
                self.message("gameEvent: called outside _handleConnection for game %d, ignored" % game_id)
            return

        game = self.factory.getGame(game_id)
        if not game:
            if self.factory.verbose > 3:
                self.message("gameEvent: called for unknown game %d, ignored" % game_id)
            return

        if type == "end_round":
            forward_packets.append(PacketPokerEndRound(game_id = game_id))

        elif type == "end_round_last":
            forward_packets.append(PacketPokerEndRoundLast(game_id = game_id))

        elif type == "money2bet":
            ( serial, amount ) = args
            player = game.getPlayer(serial)
            last_action = game.historyGet()[-1][0]
            if ( last_action == "raise" or last_action == "call" ) :
                if not self.no_display_packets:
                    forward_packets.extend(self.updateBetLimit(game))
                if last_action == "raise":
                    forward_packets.append(PacketPokerHighestBetIncrease(game_id = game.id))
            if not self.no_display_packets:
                forward_packets.extend(self.chipsPlayer2Bet(game, player, amount))

        elif type == "bet2pot":
            ( serial, amount ) = args
            if not self.no_display_packets and game.isBlindAnteRound():
                player = game.getPlayer(serial)
                forward_packets.extend(self.chipsBet2Pot(game, player, amount, 0))

        elif type == "round_cap_decrease":
            if not self.no_display_packets:
                forward_packets.extend(self.updateBetLimit(game))

    def setPlayerDelay(self, game, serial, value):
        player = game.getPlayer(serial)
        if player == None:
            print "WARNING setPlayerDelay for a non-existing player %d" % serial
        else:
            player.getUserData()['delay'] = time.time() + value

    def getPlayerDelay(self, game, serial):
        if not game: return 0
        player = game.getPlayer(serial)
        if not player: return 0
        user_data = player.getUserData()
        if not user_data or not user_data.has_key('delay'): return 0
        return user_data['delay']

    def canHandlePacket(self, packet):
        if not self.factory.isAlwaysHandled(packet) and hasattr(packet, "game_id") and hasattr(packet, "serial"):
            delay = self.getPlayerDelay(self.factory.packet2game(packet), packet.serial)
            if delay <= time.time():
                return ( True, 0 )
            else:
                return ( False, delay )
        else:
            return ( True, 0 )

    def resendPlayerTimeoutWarning(self, game):
        if game.isRunning() and game.getSerialInPosition() == self.getSerial():
            player = game.getPlayer(self.getSerial())
            if player.user_data['timeout']:
                ( when, timeout ) = player.user_data['timeout']
                now = time.time()
                timeout = timeout - ( now - when )
                if timeout > 0:
                    return ( PacketPokerTimeoutWarning(game_id = game.id,
                                                       serial = self.getSerial(),
                                                       timeout = int(timeout),
                                                       when = int(now) ), )
        return ()
        
    def setPlayerTimeout(self, game, packet):
        packet.timeout -= int(self.getLag())
        if packet.timeout > 0:
            packet.when = int(time.time())
            player = game.getPlayer(packet.serial)
            player.getUserData()['timeout'] = ( packet.when, packet.timeout )
            return True
        else:
            return False
        
    def unsetPlayerTimeout(self, game, serial):
        player = game.getPlayer(serial)
        player.getUserData()['timeout'] = None
    
    def postMuck(self, game, want_to_muck):
        if game:            
            packet_type = want_to_muck and PacketPokerMuckAccept or PacketPokerMuckDeny
            self.sendPacket(packet_type(game_id = game.id, 
                                        serial  = self.getSerial()) )
    
    def _handleConnection(self, packet):
        if self.factory.verbose > 3: self.message("PokerClientProtocol:handleConnection: %s" % packet )
        
        self.forward_packets = [ packet ]
        forward_packets = self.forward_packets
        
        if packet.type == PACKET_POKER_USER_INFO:
            self.handleUserInfo(packet)

        elif packet.type == PACKET_POKER_PERSONAL_INFO:
            self.handlePersonalInfo(packet)

        elif packet.type == PACKET_POKER_TABLE:
            if packet.id == 0:
                self.error("Too many open tables")
            else:
                new_game = self.factory.getOrCreateGame(packet.id)
                new_game.prefix = self._prefix
                new_game.name = packet.name
                new_game.setTime(0)
                new_game.setVariant(packet.variant)
                new_game.setBettingStructure(packet.betting_structure)
                new_game.setMaxPlayers(packet.seats)
                new_game.reset()
                new_game.registerCallback(self.gameEvent)
                new_game.level_skin = packet.skin
                new_game.currency_serial = packet.currency_serial
                self.setCurrentGameId(new_game.id)
                self.updatePotsChips(new_game, [])
                self.position_info[new_game.id] = [ 0, 0 ]
                self.forward_packets.append(self.currentGames())

        elif packet.type == PACKET_POKER_PLAYERS_LIST:
            pass

        elif packet.type == PACKET_AUTH_REFUSED:
            pass

        elif packet.type == PACKET_AUTH_OK:
            pass
        
        elif packet.type == PACKET_SERIAL:
            self.handleSerial(packet)
            self.sendPacket(PacketPokerGetPlayerInfo())

        elif packet.type == PACKET_POKER_PLAYER_INFO:
            self.handlePlayerInfo(packet)

        elif packet.type == PACKET_ERROR:
            self.error("Server reported error : %s" % packet)
            return

        game = self.factory.packet2game(packet)

        if game and packet.type == PACKET_POKER_TABLE_DESTROY:
            self.scheduleTableQuit(game)
            game = None

        #
        # It is possible to receive packets related to a game that we know nothing
        # about after quitting a table. When quitting a table the client deletes
        # all information related to the game without waiting confirmation from
        # the server. Therefore the server may keep sending packets related to
        # the game before noticing TABLE_QUIT packet.
        #
        if game:
            if packet.type == PACKET_POKER_START:
                if packet.hand_serial == 0:
                    self.error("game start was refused")
                    forward_packets.remove(packet)
                elif game.isRunning():
                    raise UserWarning, "you should not be here (state: %s)" % game.state
                else:
                    game.setTime(packet.time)
                    game.setHandsCount(packet.hands_count)
                    game.setLevel(packet.level)
                    game.beginTurn(packet.hand_serial)
                    self.position_info[game.id][POSITION_OBSOLETE] = True
                    if not self.no_display_packets:
                        forward_packets.append(PacketPokerBoardCards(game_id = game.id, serial = self.getSerial()))
                        for serial in game.player_list:
                            forward_packets.append(self.updatePlayerChips(game, game.serial2player[serial]))
                        forward_packets.extend(self.updatePotsChips(game, []))

            elif packet.type == PACKET_POKER_CANCELED:
                if not self.no_display_packets and packet.amount > 0:
                    player = game.getPlayer(packet.serial)
                    if player.bet > 0:
                        forward_packets.extend(self.chipsBet2Pot(game, player, player.bet, 0))
                    if packet.amount > 0:
                        forward_packets.append(self.chipsPot2Player(game, player, packet.amount, 0, "canceled"))
                game.canceled(packet.serial, packet.amount)
                forward_packets.append(PacketPokerPosition(game_id = game.id))

            elif packet.type == PACKET_POKER_PLAYER_ARRIVE:
                game.addPlayer(packet.serial, packet.seat)
                player = game.getPlayer(packet.serial)
                player.setUserData(DEFAULT_PLAYER_USER_DATA.copy())
                player.name = packet.name
                player.url = packet.url
                player.outfit = packet.outfit
                player.auto_blind_ante = packet.auto_blind_ante
                player.wait_for = packet.wait_for
                player.auto = packet.auto
                if not self.no_display_packets:
                    self.forward_packets.append(PacketPokerSeats(game_id = game.id,
                                                                 seats = game.seats()))

            elif ( packet.type == PACKET_POKER_PLAYER_LEAVE or
                   packet.type == PACKET_POKER_TABLE_MOVE ) :
                game.removePlayer(packet.serial)
                forward_packets.remove(packet)
                forward_packets.append(PacketPokerPlayerLeave(game_id = packet.game_id,
                                                              serial = packet.serial,
                                                              seat = packet.seat))
                if not self.no_display_packets:
                    self.forward_packets.append(PacketPokerSeats(game_id = game.id,
                                                                 seats = game.seats()))

            elif packet.type == PACKET_POKER_PLAYER_SELF:
                ( serial_in_position, position_is_obsolete ) = self.position_info[game.id]
                if serial_in_position == self.getSerial():
                    self.position_info[game.id] = [ 0, True ]
                forward_packets.extend(self.updateBetLimit(game))

            elif packet.type == PACKET_POKER_POSITION:
                if game.isBlindAnteRound():
                    game.setPosition(packet.position)
                forward_packets.remove(packet)

            elif packet.type == PACKET_POKER_SEAT:
                if packet.seat == -1:
                    self.error("This seat is busy")
                else:
                    if game.isTournament():
                        self.sendPacket(PacketPokerSit(serial = self.getSerial(),
                                                       game_id = game.id))

            elif packet.type == PACKET_POKER_LOOK_CARDS:
                pass

            elif packet.type == PACKET_POKER_SEATS:
                forward_packets.remove(packet)
                #game.setSeats(packet.seats)

            elif packet.type == PACKET_POKER_PLAYER_CARDS:
                player = game.getPlayer(packet.serial)
                player.hand.set(packet.cards)
                #if not self.no_display_packets:
                #    forward_packets.remove(packet)

            elif packet.type == PACKET_POKER_BOARD_CARDS:
                game.board.set(packet.cards)

            elif packet.type == PACKET_POKER_DEALER:
                game.setDealer(packet.dealer)

            elif packet.type == PACKET_POKER_SIT_OUT:
                game.sitOut(packet.serial)

            elif packet.type == PACKET_POKER_AUTO_FOLD:
                game.autoPlayer(packet.serial)

            elif packet.type == PACKET_POKER_AUTO_BLIND_ANTE:
                game.autoBlindAnte(packet.serial)

            elif packet.type == PACKET_POKER_NOAUTO_BLIND_ANTE:
                game.noAutoBlindAnte(packet.serial)

            elif packet.type == PACKET_POKER_MUCK_REQUEST:                
                game.setMuckableSerials(packet.muckable_serials)
                if packet.game_id != self.getCurrentGameId():
                   self.postMuck(game, True)
                
            elif packet.type == PACKET_POKER_SIT:
                game.sit(packet.serial)

            elif packet.type == PACKET_POKER_TIMEOUT_WARNING:
                if not self.setPlayerTimeout(game, packet):
                    forward_packets.remove(packet)
            
            elif packet.type == PACKET_POKER_TIMEOUT_NOTICE:
                self.unsetPlayerTimeout(game, packet.serial)

            elif packet.type == PACKET_POKER_WAIT_FOR:
                game.getPlayer(packet.serial).wait_for = packet.reason
                forward_packets.remove(packet)

            elif packet.type == PACKET_POKER_IN_GAME:
                for serial in game.serialsAll():
                    player = game.getPlayer(serial)
                    wait_for = player.wait_for
                    in_game = serial in packet.players 
                    if in_game or wait_for:
                        if not game.isSit(serial):
                            game.sit(serial)
                            forward_packets.append(PacketPokerSit(game_id = game.id,
                                                                  serial = serial))
                        if wait_for:
                            if wait_for == True and not in_game and not game.isRunning():
                                #
                                # A player is waiting for the blind (big, late...)
                                # and the server says it will not participate to the
                                # blindAnte round. This only happens when the anteRound
                                # is already finished on the server (i.e. when connecting
                                # to a table in the middle of a game). 
                                #
                                player.wait_for = "first_round"
                            else:
                                player.wait_for = wait_for
                            forward_packets.append(PacketPokerWaitFor(game_id = game.id,
                                                                      serial = serial,
                                                                      reason = wait_for))
                    else:
                        if game.isSit(serial):
                            game.sitOut(serial)                            
                            forward_packets.append(PacketPokerSitOut(game_id = game.id,
                                                                     serial = serial))

            elif packet.type == PACKET_POKER_RAKE:
                game.setRakedAmount(packet.value)
                
            elif packet.type == PACKET_POKER_WIN:
                if not self.no_display_packets:
                    for serial in packet.serials:
                        forward_packets.append(PacketPokerPlayerWin(serial = serial, game_id = game.id))

                if game.winners:
                    #
                    # If we know the winners before an explicit call to the distributeMoney
                    # method, it means that there is no showdown.
                    #
                    if not self.no_display_packets:
                        if game.isGameEndInformationValid():
                            forward_packets.append(PacketPokerShowdown(game_id = game.id, showdown_stack = game.showdown_stack))
                        forward_packets.extend(self.packetsPot2Player(game))
                else:
                    game.distributeMoney()

                    winners = game.winners[:]
                    winners.sort()
                    packet.serials.sort()
                    if winners != packet.serials:
                        raise UserWarning, "game.winners %s != packet.serials %s" % (winners, packet.serials)
                    if not self.no_display_packets:
                        if game.isGameEndInformationValid():
                            forward_packets.extend(self.packetsShowdown(game))
                            forward_packets.append(PacketPokerShowdown(game_id = game.id, showdown_stack = game.showdown_stack))

                        forward_packets.extend(self.packetsPot2Player(game))
                    game.endTurn()
                forward_packets.append(PacketPokerPosition(game_id = game.id))

            elif packet.type == PACKET_POKER_REBUY:
                forward_packets.remove(packet)
                game.rebuy(packet.serial, packet.amount)
                player = game.getPlayer(packet.serial)
                chips = PacketPokerPlayerChips(game_id = game.id,
                                               serial = packet.serial,
                                               money = player.money,
                                               bet = player.bet)
                forward_packets.append(chips)

            elif packet.type == PACKET_POKER_PLAYER_CHIPS:
                player = game.getPlayer(packet.serial)
                if player.buy_in_payed:
                    if player.bet != packet.bet:
                        if self.factory.verbose > 1:
                            self.error("server says player %d has a bet of %d chips and client thinks it has %d" % ( packet.serial, packet.bet, player.bet))
                        player.bet = packet.bet
                    if player.money != packet.money:
                        if self.factory.verbose > 1:
                            self.error("server says player %d has a money of %d chips and client thinks it has %d" % ( packet.serial, packet.money, player.money))
                        player.money = packet.money
                else:
                    #
                    # If server sends chips amount for a player that did not yet pay the buy in
                    # 
                    player.bet = packet.bet
                    player.money = packet.money
                    if player.money > 0:
                        player.buy_in_payed = True

            elif packet.type == PACKET_POKER_FOLD:
                game.fold(packet.serial)
                if game.isSitOut(packet.serial):
                    forward_packets.append(PacketPokerSitOut(game_id = game.id,
                                                             serial = packet.serial))

            elif packet.type == PACKET_POKER_CALL:
                game.call(packet.serial)

            elif packet.type == PACKET_POKER_RAISE:
                game.callNraise(packet.serial, packet.amount)

            elif packet.type == PACKET_POKER_CHECK:
                game.check(packet.serial)

            elif packet.type == PACKET_POKER_BLIND:
                player = game.getPlayer(packet.serial)
                game.blind(packet.serial, packet.amount, packet.dead)

            elif packet.type == PACKET_POKER_BLIND_REQUEST:
                game.setPlayerBlind(packet.serial, packet.state)

            elif packet.type == PACKET_POKER_ANTE:
                player = game.getPlayer(packet.serial)
                game.ante(packet.serial, packet.amount)

            elif packet.type == PACKET_POKER_STATE:
                self.position_info[game.id][POSITION_OBSOLETE] = True

                if game.isBlindAnteRound():
                    game.blindAnteRoundEnd()

                if packet.string == "end":
                    game.endState()

                #
                # A state change is received at the begining of each
                # betting round. No state change is received when
                # reaching showdown or otherwise terminating the hand.
                #
                if game.isFirstRound():
                    game.initRound()
                else:
                    if not self.no_display_packets:
                        if ( packet.string == "end" and
                             game.isSingleUncalledBet(game.side_pots) ):
                            forward_packets.extend(self.moveBet2Player(game))
                        else:
                            forward_packets.extend(self.moveBet2Pot(game))

                    if packet.string != "end":
                        game.initRound()

                if not self.no_display_packets:
                    if game.isRunning() and game.cardsDealt() and game.downCardsDealtThisRoundCount() > 0:
                        forward_packets.append(PacketPokerDealCards(game_id = game.id,
                                                                    numberOfCards = game.downCardsDealtThisRoundCount(),
                                                                    serials = game.serialsNotFold()))

                if game.isRunning() and game.cardsDealt() and game.cardsDealtThisRoundCount() :
                    for player in game.playersNotFold():
                        cards = player.hand.toRawList()
                        forward_packets.append(PacketPokerPlayerCards(game_id = game.id,
                                                                      serial = player.serial,
                                                                      cards = cards))

                if ( packet.string != "end" and not game.isBlindAnteRound() ):
                    if not self.no_display_packets:
                        forward_packets.extend(self.updateBetLimit(game))
                    forward_packets.append(PacketPokerBeginRound(game_id = game.id))

                if game.state != packet.string:
                    self.error("state = %s, expected %s instead " % ( game.state, packet.string ))


            ( serial_in_position, position_is_obsolete ) = self.position_info[game.id]
            if game.isRunning():
                position_changed = serial_in_position != game.getSerialInPosition()
                if position_is_obsolete or position_changed:
                    self_was_in_position = self.getSerial() != 0 and serial_in_position == self.getSerial()
                    serial_in_position = game.getSerialInPosition()
                    self_in_position = serial_in_position == self.getSerial()
                    if serial_in_position > 0:
                        if position_changed:
                            forward_packets.append(PacketPokerPosition(game_id = game.id,
                                                                       serial = serial_in_position))
                        if ( self_was_in_position and not self_in_position ):
                            self.unsetPlayerTimeout(game, self.getSerial())
                            if not game.isBlindAnteRound() or not game.getPlayer(self.getSerial()).isAutoBlindAnte():
                                forward_packets.append(PacketPokerSelfLostPosition(game_id = game.id,
                                                                                   serial = serial_in_position))
                        if ( ( not self_was_in_position or position_is_obsolete ) and self_in_position ):
                            if not game.isBlindAnteRound() or not game.getPlayer(self.getSerial()).isAutoBlindAnte():
                                forward_packets.append(PacketPokerSelfInPosition(game_id = game.id,
                                                                                 serial = serial_in_position))
                    elif self_was_in_position:
                        self.unsetPlayerTimeout(game, self.getSerial())
                        if not game.isBlindAnteRound() or not game.getPlayer(self.getSerial()).isAutoBlindAnte():
                            forward_packets.append(PacketPokerSelfLostPosition(game_id = game.id,
                                                                               serial = self.getSerial()))

            else:
                if serial_in_position > 0:
                    if not game.isBlindAnteRound() or not game.getPlayer(self.getSerial()).isAutoBlindAnte():
                        forward_packets.append(PacketPokerSelfLostPosition(game_id = game.id,
                                                                           serial = self.getSerial()))
                    serial_in_position = 0
            position_is_obsolete = False
            self.position_info[game.id] = [ serial_in_position, position_is_obsolete ]

        for forward_packet in forward_packets:
            self.schedulePacket(forward_packet)
        self.forward_packets = None

    def moveBet2Pot(self, game):
        packets = []
        round_contributions = game.getLatestPotContributions()
        for (pot_index, pot_contribution) in round_contributions.iteritems():
            for (serial, amount) in pot_contribution.iteritems():
                player = game.getPlayer(serial)
                packets.extend(self.chipsBet2Pot(game, player, amount, pot_index))

        packets.extend(self.updatePotsChips(game, game.getPots()))
        return packets
        
    #
    # Should be move all bets back to players (for uncalled bets)
    # This is a border case we don't want to handle right now
    #
    moveBet2Player = moveBet2Pot
        
    def updateBetLimit(self, game):
        if ( self.getSerial() not in game.serialsPlaying() or
             game.isBlindAnteRound() ):
            return []
            
        packets = []
        serial = self.getSerial()
        (min_bet, max_bet, to_call) = game.betLimits(serial)
        found = None
        steps = self.factory.chips_values[:]
        steps.reverse()
        #
        # Search for the lowest chip value by which all amounts can be divided
        #
        for step in steps:
            if min_bet % step == 0 and max_bet % step == 0 and to_call % step == 0:
                found = step
        if found:
            if self.factory.verbose:
                self.message(" => bet min=%d, max=%d, step=%d, to_call=%d" % ( min_bet, max_bet, found, to_call))
            packets.append(PacketPokerBetLimit(game_id = game.id,
                                               min = min_bet,
                                               max = max_bet,
                                               step = game.getChipUnit(),
                                               call = to_call,
                                               allin = game.getPlayer(serial).money,
                                               pot = game.potAndBetsAmount() + to_call * 2))
        else:
            self.error("no chip value (%s) is suitable to step from min_bet = %d to max_bet = %d" % ( self.factory.chips_values, min_bet, max_bet ))
        return packets

    def currentGames(self, exclude = None):
        games = self.factory.games.keys()
        if exclude:
            games.remove(exclude)
        return PacketPokerCurrentGames(game_ids = games,
                                       count = len(games))
    
    def packetsPot2Player(self, game):
        packets = []
        current_pot = 0
        game_state = game.showdown_stack[0]
        pots = game_state['side_pots']['pots']
        frame_count = len(game.showdown_stack) - 1
        for frame in game.showdown_stack:
            if frame['type'] == 'left_over':
                player = game.getPlayer(frame['serial'])
                packets.append(self.chipsPot2Player(game, player, frame['chips_left'], len(pots) - 1, "left_over"))
            elif frame['type'] == 'uncalled':
                player = game.getPlayer(frame['serial'])
                packets.append(self.chipsPot2Player(game, player, frame['uncalled'], len(pots) - 1, "uncalled"))
            elif frame['type'] == 'resolve':
                cumulated_pot_size = 0
                next_pot = current_pot
                for (pot_size, pot_total) in pots[current_pot:]:
                    cumulated_pot_size += pot_size
                    next_pot += 1
                    if cumulated_pot_size >= frame['pot']:
                        break
                if cumulated_pot_size != frame['pot']:
                    self.error("pot %d, total size = %d, expected %d" % ( current_pot, cumulated_pot_size, frame['pot'] ))
                merged_pot = next_pot - 1
                if merged_pot > current_pot:
                    merge = PacketPokerChipsPotMerge(game_id = game.id,
                                                     sources = range(current_pot, merged_pot),
                                                     destination = merged_pot)
                    if self.factory.verbose > 2:
                        self.message("packetsPot2Player: %s" % merge)
                    packets.append(merge)
                if frame_count == 1 and len(frame['serial2share']) == 1:
                    #
                    # Happens quite often : single winner. Special case where
                    # we use the exact chips layout saved in game_state.
                    #
                    serial = frame['serial2share'].keys()[0]
                    packets.append(self.chipsPot2Player(game, game.getPlayer(serial), game_state['pot'], merged_pot, "win"))
                else:
                    #
                    # Possibly complex showdown, cannot avoid breaking chip stacks
                    #
                    for (serial, share) in frame['serial2share'].iteritems():
                        packets.append(self.chipsPot2Player(game, game.getPlayer(serial), share, merged_pot, "win"))
                current_pot = next_pot
            else:
                pass
                
        for player in game.serial2player.itervalues():
            packets.append(self.updatePlayerChips(game, player))
        packets.extend(self.updatePotsChips(game, []))
        return packets
        
    def packetsShowdown(self, game):
        if not game.isGameEndInformationValid():
            return []

        game_state = game.showdown_stack[0]
        delta_max = -1
        serial_delta_max = -1
        for (serial, delta) in game_state['serial2delta'].iteritems():
            if delta_max < delta:
                delta_max = delta
                serial_delta_max = serial
        
        packets = []
        if game.variant == "7stud":
            for player in game.playersAll():
                packets.append(PacketPokerPlayerNoCards(game_id = game.id,
                                                        serial = player.serial))
                if player.hand.areVisible():
                    packet = PacketPokerPlayerCards(game_id = game.id,
                                                    serial = player.serial,
                                                    cards = player.hand.tolist(True))
                    packet.visibles = "hole"
                    packets.append(packet)

        for (serial, best) in game.serial2best.iteritems():
            for (side, (value, bestcards)) in best.iteritems():
                if serial in game.side2winners[side]:
                    if len(bestcards) > 1:
                        side = game.isHighLow() and side or ""
                        if side == "low":
                            hand = ""
                        else:
                            hand = game.readableHandValueShort(side, bestcards[0], bestcards[1:])
                        cards = game.getPlayer(serial).hand.toRawList()
                        best_hand = 0
                        if serial == serial_delta_max:
                            best_hand = 1
                        packets.append(PacketPokerBestCards(game_id = game.id,
                                                            serial = serial,
                                                            side = side,
                                                            cards = cards,
                                                            bestcards = bestcards[1:],
                                                            board = game.board.tolist(True),
                                                            hand = hand,
                                                            besthand = best_hand))
        return packets

    def connectionLost(self, reason):
        if self.factory.verbose:
            print "connectionLost: noticed, aborting all tables."
        self.abortAllTables()
        UGAMEClientProtocol.connectionLost(self, reason)
        
    def abortAllTables(self):
        for game in self.factory.games.values():
            self.scheduleTableAbort(game)

    def scheduleTableAbort(self, game):
        game_id = game.id
        def thisgame(packet):
            return hasattr(packet, "game_id") and packet.game_id == game_id
        self.unschedulePackets(thisgame)
        self.discardPackets(game_id)
        self.scheduleTableQuit(game)

    def scheduleTableQuit(self, game):
        self.schedulePacket(PacketPokerBatchMode(game_id = game.id))
        for player in game.playersAll():
            packet = PacketPokerPlayerLeave(game_id = game.id,
                                            serial = player.serial,
                                            seat = player.seat)
            self.schedulePacket(packet)
        self.schedulePacket(PacketPokerStreamMode(game_id = game.id))
        self.schedulePacket(PacketPokerTableQuit(game_id = game.id,
                                                serial = self.getSerial()))
        self.schedulePacket(self.currentGames(game.id))
        self.publishAllPackets()

    def resendPackets(self, game_id):
        self.publishAllPackets()
        game = self.getGame(game_id)
        self.setCurrentGameId(game.id)
        packets = []
        packet = PacketPokerTable(id = game.id,
                                  name = game.name,
                                  variant = game.variant,
                                  seats = game.max_players,
                                  betting_structure = game.betting_structure,
                                  players = game.allCount(),
                                  # observers ?
                                  # waiting ?
                                  # player_timeout ?
                                  # muck_timeout ?
                                  hands_per_hour = game.stats["hands_per_hour"],                                  
                                  average_pot = game.stats["average_pot"],
                                  percent_flop = game.stats["percent_flop"],
                                  skin = game.level_skin
                                  )
        packets.append(PacketPokerBatchMode(game_id = game.id))
        packet.seats_all = game.seats_all
        packets.append(packet)
        packets.append(PacketPokerDealer(game_id = game.id, dealer = game.dealer_seat))
        for player in game.playersAll():
            packets.append(PacketPokerPlayerArrive(game_id = game.id,
                                                   serial = player.serial,
                                                   name = player.name,
                                                   url = player.url,
                                                   outfit = player.outfit,
                                                   blind = player.blind,
                                                   remove_next_turn = player.remove_next_turn,
                                                   sit_out = player.sit_out,
                                                   sit_out_next_turn = player.sit_out_next_turn,
                                                   auto = player.auto,
                                                   auto_blind_ante = player.auto_blind_ante,
                                                   wait_for = player.wait_for,
                                                   seat = player.seat))
            if player.isSit():
                packets.append(PacketPokerSit(game_id = game.id,
                                              serial = player.serial))
            else:
                packets.append(PacketPokerSitOut(game_id = game.id,
                                                 serial = player.serial))
            packets.append(self.updatePlayerChips(game, player))
        packets.append(PacketPokerSeats(game_id = game.id,
                                        seats = game.seats()))
        packets.append(PacketPokerStart(game_id = game.id,
                                        hand_serial = game.hand_serial))
        if game.isRunning():
            players_with_cards = game.playersNotFold()
        elif  game.isGameEndInformationValid():
            players_with_cards = game.playersWinner()
        else:
            players_with_cards = []

        if players_with_cards:
            for player in players_with_cards:
                packet = PacketPokerPlayerCards(game_id = game.id,
                                                serial = player.serial,
                                                cards = player.hand.toRawList())
                packets.append(packet)
            packets.append(PacketPokerBoardCards(game_id = game.id,
                                                 cards = game.board.tolist(False)))
        if game.isRunning():
            if not self.no_display_packets:
                packets.extend(self.updatePotsChips(game, game.getPots()))
            packets.append(PacketPokerPosition(game_id = game.id,
                                               serial = game.getSerialInPosition()))
            if not self.no_display_packets:
                packets.extend(self.updateBetLimit(game))
        else:
            if not self.no_display_packets and game.isGameEndInformationValid():
                packets.extend(self.packetsShowdown(game))
                packets.append(PacketPokerShowdown(game_id = game.id, showdown_stack = game.showdown_stack))
        packets.append(PacketPokerStreamMode(game_id = game.id))
        packets.extend(self.resendPlayerTimeoutWarning(game))
        
        for packet in packets:
            self.schedulePacket(packet)

    def deleteGames(self):
        self.setCurrentGameId(None)
        for game_id in self.factory.games.keys():
            self.deleteGame(game_id)
        
    def deleteGame(self, game_id):
        if self.factory.verbose > 2: self.message("deleteGame: %d" % game_id)
        if self.position_info.has_key(game_id):
            del self.position_info[game_id]
        else:
            print "CRITICAL: no position_info for game %d" % game_id
        self.factory.deleteGame(game_id)
        def thisgame(packet):
            return hasattr(packet, "game_id") and packet.game_id == game_id
        self.unschedulePackets(thisgame)
        self.discardPackets(game_id)

    def getGame(self, game_id):
        return self.factory.getGame(game_id)

    def sendPacket(self, packet):
        if packet.type == PACKET_POKER_TABLE_QUIT:
            self.scheduleTableAbort(self.getGame(packet.game_id))
        elif packet.type == PACKET_POKER_SIT_OUT:
            game = self.getGame(packet.game_id)
            if game:
                game.sitOutNextTurn(packet.serial)
            self.schedulePacket(PacketPokerSitOutNextTurn(game_id = packet.game_id,
                                                          serial = packet.serial))
        elif packet.type == PACKET_POKER_SIT:
            game = self.getGame(packet.game_id)
            if game:
                game.sitRequested(packet.serial)
            self.schedulePacket(PacketPokerSitRequest(game_id = packet.game_id,
                                                      serial = packet.serial))
        elif packet.type == PACKET_QUIT:
            self.ignoreIncomingData()
            self.abortAllTables()

        UGAMEClientProtocol.sendPacket(self, packet)

    def protocolEstablished(self):
        self.setPingDelay(self.factory.ping_delay)
        self.user.name = self.factory.name
        self.user.password = self.factory.password
        self._packet2id = self.packet2id
        self._packet2front = self.packet2front
        self.schedulePacket(PacketBootstrap())
        UGAMEClientProtocol.protocolEstablished(self)

    def packet2front(self, packet):
        if ( hasattr(packet, "game_id") and
             self.getGame(packet.game_id) ):
            if ( packet.type == PACKET_POKER_CHAT and
                 not match("^Dealer:", packet.message) ):
                return True

            elif ( packet.type == PACKET_POKER_PLAYER_ARRIVE and
                   packet.serial == self.getSerial() ):
                return True

        return False

    def registerLagmax(self, method):
        self.lagmax_callbacks.append(method)

    def unregisterLagmax(self, method):
        self.lagmax_callbacks.remove(method)
        
    def triggerLagmax(self, packet):
        for method in self.lagmax_callbacks:
            method(packet)
    
    def packet2id(self, packet):
        self.triggerLagmax(packet)
        if not self.factory.isOutbound(packet) and hasattr(packet, "game_id"):
            return packet.game_id
        elif packet.type == PACKET_POKER_TABLE:
            return packet.id
        else:
            return 0
        
    def protocolInvalid(self, server, client):
        self.schedulePacket(PacketProtocolError(message = "Upgrade the client from\nhttp://mekensleep.org/\nServer version is %s\nClient version is %s" % ( server, client ) ))
        self.publishAllPackets()
        UGAMEClientProtocol.protocolInvalid(self)

    def publishDelay(self, delay):
        if self.factory.verbose > 2: self.message("publishDelay: %f delay" % delay)
        publish_time = time.time() + delay
        if publish_time > self.publish_time:
            self.publish_time = publish_time
            
    def schedulePacket(self, packet):
        if not self.factory.isOutbound(packet) and hasattr(packet, "game_id") and not self.factory.gameExists(packet.game_id):
            return
        self.publish_packets.append(packet)
        if not self._poll:
            self.publishPacket()
            
    def unschedulePackets(self, predicate):
        self.publish_packets = filter(lambda packet: not predicate(packet), self.publish_packets)
        
    def publishPackets(self):
        if not self._poll:
            return
        
        delay = 0.01
        packets_len = len(self.publish_packets)
        if packets_len > 0:
            #
            # If time has not come, make sure we are called at a later time
            # to reconsider the situation
            #
            wait_for = self.publish_time - time.time()
            if wait_for > 0:
                if self.factory.verbose > 2:
                    self.message("publishPackets: %f before next packet is sent" % wait_for)
                    delay = wait_for
                    self.block()
            else:
                self.publishPacket()
                if packets_len > 0:
                    self.block()
                else:
                    self.unblock()
        else:
            self.unblock()
            
        if not self.publish_timer or not self.publish_timer.active():
            self.publish_timer = reactor.callLater(delay, self.publishPackets)

    def publishPacket(self):
        packet = self.publish_packets[0]
        if not self.established and not self.factory.isConnectionLess(packet):
            if self.factory.verbose > 5:
                print "publishPacket: skip because connection not established"
            return
        self.publish_packets.pop(0)
        what = 'outbound'
        if hasattr(packet, "game_id"):
            if self.factory.isOutbound(packet):
                what = 'outbound'
            else:
                if packet.game_id == self.getCurrentGameId():
                    what = 'current'
                else:
                    what = 'not_current'
        elif ( packet.type == PACKET_POKER_TABLE or
               packet.type == PACKET_POKER_TABLE_QUIT ):
            what = 'current'
        else:
            what = 'outbound'

        if self.factory.verbose > 2: self.message("publishPacket: %s: %s" % ( what, packet ) )
        if self.callbacks[what].has_key(packet.type):
            callbacks = self.callbacks[what][packet.type]
            for callback in callbacks:
                callback(self, packet)
        
    def publishAllPackets(self):
        while len(self.publish_packets) > 0:
            self.publishPacket()
