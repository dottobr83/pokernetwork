#
# Copyright (C) 2005 Mekensleep
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
#
from re import match

from twisted.python import dispatch

from pokernetwork.pokerchildren import PokerRsync, RSYNC_DONE

class Constants:
    EXCLUDES = []
    UPGRADES_DIR = None
    BANDWIDTH = [ "--bwlimit=128" ]
    
TICK = "//event/pokernetwork/upgrade/tick"
NEED_UPGRADE = "//event/pokernetwork/upgrade/need_upgrade"
CLIENT_VERSION_OK = "//event/pokernetwork/upgrade/client_version_ok"
UPGRADE_READY = "//event/pokernetwork/upgrade/upgrade_ready"

class CheckClientVersion(PokerRsync):

    def __init__(self, config, settings, version, callback):
        PokerRsync.__init__(self, config, settings, [ "rsync", "@SOURCE@/*" ])
        self.version_compare = "%03d%03d%03d" % version
        self.version = version
        if self.verbose > 1:
            print "CheckClientVersion checking version %s against server" % str(self.version)
        self.callback = callback
        self.spawn()
        self.need_upgrade = False

    def line(self, line):
        result = match(".* (\d+).(\d+).(\d+)$", line)
        if result:
            result = tuple(map(int, result.groups()))
            if self.verbose > 2:
                print "compare %s against %s" % ( str(result), str(self.version))
            version = "%03d%03d%03d" % result
            if version > self.version_compare:
                self.version = result
                self.need_upgrade = True

    def done(self):
        self.callback(self.need_upgrade, "%d.%d.%d" % self.version)

DRY_RUN_DONE = "//event/pokernetwork/upgrade/dry_run_done"

class DryrunUpgrade(PokerRsync):

    def __init__(self, config, settings, version):
        PokerRsync.__init__(self, config, settings, [ "rsync" ] + Constants.EXCLUDES + [ "--dry-run", "-av", "--delete", "--progress", "--log-format=FILE:%f", "@SOURCE@/" + version, "@TARGET@" ])
        self.files_count = 0
        self.files_total = 0.0

    def spawn(self):
        self.publishEvent(TICK, 0.0, "Looking for the new client upgrade")
        PokerRsync.spawn(self)
        
    def line(self, line):
        if match("^FILE:", line):
            self.files_count += 1
            if self.files_total > 0.0:
                self.publishEvent(TICK, self.files_count / self.files_total, None)
        else:
            result = match(".*?(\d+)\s+files\s+to\s+consider", line)
            if result:
                self.files_total = float(result.group(1))

    def done(self):
        self.publishEvent(TICK, 1.0, None)
        self.publishEvent(DRY_RUN_DONE, float(self.files_count))

GET_PATCH_DONE = "//event/pokernetwork/upgrade/get_patch_done"

class GetPatch(PokerRsync):

    def __init__(self, config, settings, version, files_total):
        PokerRsync.__init__(self, config, settings, [ "rsync" ] + Constants.EXCLUDES + Constants.BANDWIDTH + [ "--only-write-batch=%s/patch" % Constants.UPGRADES_DIR, "--delete", "-a", "--log-format=FILE:%f", "@SOURCE@/" + version + "/*", "@TARGET@/" ])
        self.files_count = 0
        self.files_total = files_total

    def spawn(self):
        self.publishEvent(TICK, 0.0, "Retrieving the client upgrade")
        PokerRsync.spawn(self)
        
    def line(self, line):
        if match("^FILE:", line):
            self.files_count += 1
            if self.files_total > 0.0:
                self.publishEvent(TICK, self.files_count / self.files_total, None)

    def done(self):
        self.publishEvent(TICK, 1.0, None)
        self.publishEvent(GET_PATCH_DONE)

class Upgrader(dispatch.EventDispatcher):

    def __init__(self, config, settings):
        self.verbose = settings.headerGetInt("/settings/@verbose")
        dispatch.EventDispatcher.__init__(self)
        self.config = config
        self.settings = settings
        self.target = self.settings.headerGet("/settings/rsync/@target")
        self.upgrades = self.settings.headerGet("/settings/rsync/@upgrades")
        Constants.UPGRADES_DIR = self.target + "/" + self.upgrades
        source = self.settings.headerGet("/settings/rsync/@source")

    def checkClientVersion(self, version):
        if self.verbose > 1: print "Upgrade::checkClientVersion(" + str(version) + ")" 
        self.publishEvent(TICK, 0.0, "Checking for new client version")
        CheckClientVersion(self.config, self.settings, version, self.checkClientVersionDone)

    def checkClientVersionDone(self, need_upgrade, version):
        if need_upgrade:
            self.publishEvent(TICK, 1.0, "A new version is available")
            self.publishEvent(NEED_UPGRADE, version)
        else:
            self.publishEvent(TICK, 1.0, "Ok")
            self.publishEvent(CLIENT_VERSION_OK)

    def getUpgrade(self, version, excludes):
        Constants.EXCLUDES = map(lambda pattern: "--exclude=" + pattern, (self.upgrades, "poker.client.xml") + excludes)        
        self.upgradeStage1(version)

    def upgradeStage1(self, version):
        if self.verbose > 1: print "Upgrade::upgrade to version " + version
        stage1 = DryrunUpgrade(self.config, self.settings, version)
        stage1.registerHandler(TICK, lambda ratio, message: self.publishEvent(TICK, ratio, message))
        stage1.registerHandler(DRY_RUN_DONE, lambda files_count: self.upgradeStage2(version, files_count))
        stage1.spawn()

    def upgradeStage2(self, version, files_count):
        self.publishEvent(TICK, 0.0, "Upgrading the upgrade system")
        rsync = PokerRsync(self.config, self.settings, [ "rsync" ] + Constants.EXCLUDES + Constants.BANDWIDTH + [ "--delete", "-a", "@SOURCE@/" + "%s/%s/" % ( version, self.upgrades ), Constants.UPGRADES_DIR + "/" ])
        rsync.registerHandler(RSYNC_DONE, lambda: self.upgradeStage3(version, files_count))
        rsync.spawn()

    def upgradeStage3(self, version, files_count):
        self.publishEvent(TICK, 1.0, "Upgrade system upgraded")
        stage2 = GetPatch(self.config, self.settings, version, files_count)
        stage2.registerHandler(TICK, lambda ratio, message: self.publishEvent(TICK, ratio, message))
        stage2.registerHandler(GET_PATCH_DONE, self.upgradeReady)
        stage2.spawn()
        
    def upgradeReady(self):
        if self.verbose > 1: print "Upgrade::upgradeReady"
        self.publishEvent(UPGRADE_READY, self.target, Constants.UPGRADES_DIR)
