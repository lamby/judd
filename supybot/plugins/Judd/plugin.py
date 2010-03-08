###
# Copyright (c) 2007,2008, Mike O'Connor
# Copyright (c) 2010,      Stuart Prescott
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

###

#TODO:

import supybot.conf as conf
import supybot.utils as utils
from supybot.commands import *
import supybot.plugins as plugins
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
from debian_bundle import debian_support

import re
import psycopg2
import psycopg2.extras

import os
#import gzip
#import time
#import popen2
import fnmatch
import subprocess
#from supybot.utils.iter import all, imap, ifilter
from PackageFileList import PackageFileList

release_map = { 'unstable':'sid', 'testing':'squeeze', 'stable':'lenny' }
releases = [ 'etch', 'etch-backports', 'etch-multimedia', 'etch-security', 'etch-volatile', 'experimental', 'lenny', 'lenny-multimedia', 'lenny-security', 'lenny-backports', 'lenny-volatile', 'squeeze', 'squeeze-security', 'squeeze-multimedia', 'sid', 'sid-multimedia', 'unstable', 'testing', 'stable' ]

arches = [ 'alpha', 'amd64', 'arm', 'armel', 'hppa', 'hurd-i386', 'i386', 'ia64', 'm68k', 'mips', 'mipsel', 'powerpc', 's390', 'sparc', 'all' ]

def parse_standard_options( optlist, args=None ):
    # FIXME: should this default to lenny/i386 if the args are out of bounds?
    if not args:
        args=[]
    release='lenny'
    arch='i386'

    for( option,arg ) in optlist:
        if option=='release':
            release=arg;
        elif option=='arch':
            arch=arg;

    if args in releases:
        release=args
    elif args in arches:
        arch=args

    if release_map.has_key( release ):
        release = release_map[ release ]

    if not release in releases:
        release=None

    if not arch in arches:
        arch=None

    return release, arch

class Release:
    def __init__(self, dbconn, arch="i386", release="lenny", **kwargs):
        self.dbconn = dbconn
        self.arch = arch
        self.release = release
        self.cache = {}
        self.scache = {}

    def Package(self, package):
        if not self.cache.has_key(package):
            self.cache[package] = Package(self.dbconn, arch=self.arch, \
                                  release=self.release, package=package)
        return self.cache[package]

    def Source(self, package, autoBin2Src=True):
        if not self.scache.has_key(package):
            self.scache[package] = SourcePackage(self.dbconn, arch=self.arch, \
                                  release=self.release, package=package)
            if autoBin2Src and not self.scache[package].Found():
                p = self.bin2src(package)
                if p:
                    self.Source(p, False)
                    return self.scache[p]
        return self.scache[package]

    def bin2src(self, package):
        """Returns the source package for a given binary package"""
        c = self.dbconn.cursor()
        c.execute(r"""SELECT source
                      FROM packages
                      WHERE package=%(package)s
                        AND release=%(release)s LIMIT 1""",
                   dict( package=package,
                         release=self.release) );
        row = c.fetchone()
        if row:
            return row[0]
        else:
            return


class RelationChecker():
    def __init__(self, release):
        self.release = release

    def CheckInstall(self, package=None, recommends=True):
        pass

    def Check(self, package=None, relation='depends'):
        p = self.release.Package(package)
        if not p.Found():
            return None, None
        bad, good = self.CheckRelationsList(p.RelationsEntryList(relation))
        if relation == 'conflicts':
            return good, bad
        return bad, good

    def CheckRelationsList(self, relationlist):
        bad = []
        good = []
        for opts in relationlist:
            #print "Considering fragment %s" % opts
            satisfied = False
            while not satisfied and len(opts):
                item = opts.pop(0)
                #print "== part %s (%d)" % (item, len(opts))
                satisfied = self.RelationSatisfied(item['package'], \
                                          item['operator'], item['version'],
                                          item['arch'])
            if not satisfied:
                bad.append(item)
            else:
                good.append(item)
        return bad, good

    def RelationSatisfied(self, package, operator=None, depversion=None, arch=None):
        print "Checking relationship for '%s', '%s', '%s' on %s" % \
                      (package, operator, depversion, arch)
        p = self.release.Package(package)
        if not p.Found():
          return p.IsVirtual()

        version = p.data['version']
        # see policy s7.1
        # http://www.debian.org/doc/debian-policy/ch-relationships.html
        if operator:
            depver = debian_support.Version(depversion)   # version from dep line
            relver = debian_support.Version(version)      # version in archive
            print "versions comparison %s %s %s %s" % (package, relver, operator, depver)
            if operator == ">>": # strictly greater than
                return relver > depver
            if operator == ">=": # greater than or equal to
                return relver >= depver
            if operator == "=": # equal to
                return relver == depver
            if operator == "<=": # less than or equal to
                return relver >= depver
            if operator == "<<": # strictly less than
                return relver < depver
            raise ValueError("Odd relation found for dependency %s: %s %s %s" % \
                                (package, relversion, operator, depversion))
        else:
            return True

class Package:
    fields  = ['*']
    table   = 'packages'
    column  = 'package'
    def __init__(self, dbconn, arch="i386", release="lenny", package=None, version=None, **kwargs):
        if not package:
            raise ValueError("Package name not specified")
        self.dbconn = dbconn
        self.arch = arch
        self.release = release
        self.package = package
        self.version = version
        self.virtual = None
        self._ProvidersList = None
        self._Fetch()

    def _Fetch(self):
        #print "Looking for %s in %s/%s." % (self.package, self.release, self.arch)
        # TODO: have the "version" do something useful
        c = self.dbconn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        f = ','.join(self.fields)
        c.execute(r"""SELECT """ + f + """
                      FROM """ + self.table + """
                      WHERE """ + self.column + """=%(package)s
                        AND (architecture='all' OR architecture=%(arch)s)
                        AND release=%(release)s
                      ORDER BY version DESC
                      LIMIT 1""",
                   dict( package=self.package,
                         arch=self.arch,
                         release=self.release) );
        self.data = c.fetchone()

    def Found(self):
        return self.data != None

    def IsVirtual(self):
        if self.virtual == None:
            self.virtual = (self.ProvidersList() != [])
        return self.virtual

    def ProvidersList(self):
        if self._ProvidersList == None:
            # remove all characters from the package name that aren't legal in a
            # package name i.e. not in:
            #    a-z0-9-.+
            # see s5.6.1 of Debian Policy "Source" for details.
            # http://www.debian.org/doc/debian-policy/ch-controlfields.html#s-f-Source
            #
            # \m is start word boundary, \M is finish word boundary
            # (but - in package name is a word boundary)
            # \A is start string,        \Z is finish string
            # http://www.postgresql.org/docs/8.3/static/functions-matching.html
            packagere = r"(?:\A|[, ])%s(?:\Z|[, ])" % re.sub(r"[^a-z\d\-+.]", "", self.package)
            #print packagere
            c = self.dbconn.cursor()
            c.execute(r"""SELECT DISTINCT package
                          FROM packages
                          WHERE provides ~ %(package)s
                            AND (architecture='all' OR architecture=%(arch)s)
                            AND release=%(release)s""",
                      dict( package=packagere,
                            arch=self.arch,
                            release=self.release) );
            pkgs=[]
            for row in c.fetchall():
                pkgs.append( row[0] )
            self._ProvidersList = pkgs
        return self._ProvidersList

    def RelationEntry(self, relation):
        return self.data[relation]

    def RelationsEntryList(self, relation):
        if self.data[relation]:
            rels = []
            for r in re.split(r"\s*,\s*", self.data[relation]):
                rset = []
                for rel in self._SplitOptions(r):
                    #print "found rel=%s" % rel
                    p, o, v, a = self._ParseRelation(rel)
                    if a: a = a.split(' ')
                    rset.append({
                                  'relation': rel,
                                  'package':  p,
                                  'operator': o,
                                  'version':  v,
                                  'arch':     a
                                })
                rels.append(rset)
            return rels
        else:
            return []

    def _SplitOptions(self, item):
        return re.split(r"\s*\|\s*", item)

    def _ParseRelation(self, relationship):
        #print "checking item %s" % item
        m = re.match(r"""(?x)
                  (?P<package>[\w\d.+-]+)
                  (?:
                    \s*
                    \(\s*
                      (?P<rel>\>\>|\>=|=|\<\<|\<=)\s*(?P<version>[^\s]+)
                    \s*\)
                  )?
                  (?:
                    \s*
                    \[\s*
                      (?P<arch>[^]]+)
                    \s*\]
                  )?
                  """, relationship)
        if not m:
            pass        # this should never happen
        return m.group('package'), m.group('rel'), m.group('version'), m.group('arch')

class SourcePackage(Package):
    def __init__(self, dbconn, arch="i386", release="lenny", package=None, version=None, **kwargs):
        #self.fields = ['build_depends', 'build_depends_indep', 'version']
        self.table = 'sources'
        self.column = 'source'
        self.autobin2src = kwargs.get('bin2src', True)
        Package.__init__(self, dbconn, arch, release, package, version)

    def _Fetch(self):
        #print "Looking for source %s in %s/%s." % (self.package, self.release, self.arch)
        c = self.dbconn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        f = ','.join(self.fields)
        c.execute(r"""SELECT """ + f + """
                      FROM """ + self.table + """
                      WHERE """ + self.column + """=%(package)s
                        AND release=%(release)s
                      ORDER BY version DESC
                      LIMIT 1""",
                   dict( package=self.package,
                         release=self.release) );
        self.data = c.fetchone()

    def Binaries(self):
        c = self.dbconn.cursor()
        c.execute(r"""SELECT DISTINCT package
                      FROM packages
                      WHERE source=%(package)s AND release=%(release)s""",
                   dict( package=self.package,
                         arch=self.arch,
                         release=self.release) );
        pkgs = []
        for row in c.fetchall():
            pkgs.append(row[0])
        return pkgs

class Judd(callbacks.Plugin):
    """A plugin for querying a debian udd instance:  http://wiki.debian.org/UltimateDebianDatabase."""
    threaded = True

    def __init__(self, irc):
        self.__parent = super(Judd, self)
        self.__parent.__init__(irc)

        self.psql = psycopg2.connect( "dbname='%s' host='%s' port='%d' password='%s' user='%s'" %
                                      ( self.registryValue('db_database'),
                                        self.registryValue('db_hostname'),
                                        self.registryValue('db_port'),
                                        self.registryValue('db_password'),
                                        self.registryValue('db_username') ),
                                      )

        self.psql.set_isolation_level(0)


    def versions(self, irc, msg, args, package, optlist, something ):
        """<pattern> [--arch <i386>] [--release <lenny>]

        Show the available versions of a package in the optionally specified
        release and for the given architecture.
        All current releases and i386 are searched by default. By default, binary
        packages are searched; prefix the packagename with "src:" to search
        source packages.
        """
        release = None
        arch='i386'
        atleastone=False

        for( option,arg ) in optlist:
            if option=='release':
                release=arg;
            elif option=='arch':
                arch=arg;
        for option in args:
            if option in releases:
                release=option
            elif option in arches:
                arch=option
        if release_map.has_key( release ):
            release = release_map[ release ]

        c = self.psql.cursor(cursor_factory=psycopg2.extras.DictCursor)
        if package.startswith('src:'):
            packagename = package[4:]
            sql = r"""SELECT DISTINCT release,version,component
                      FROM sources
                      WHERE source=%(package)s"""
            if release:
                sql += " AND release=%(release)s"
        else:
            packagename = package
            sql = r"""SELECT DISTINCT release,version,component
                      FROM packages
                      WHERE package=%(package)s AND
                        (architecture=%(arch)s OR architecture='all')"""
            if release:
                sql += " AND release=%(release)s"

        c.execute( sql,
                    dict( package=packagename,
                          arch=arch,
                          release=release ) );

        pkgs=[]
        for row in c.fetchall():
            pkgs.append( row )

        if not pkgs:
            irc.reply( "Sorry, no package named '%s' was found." % package )
            return

        pkgs.sort( lambda a,b: debian_support.version_compare( a['version'], b['version'] ) )

        replies = []
        for row in pkgs:
            if( row['component'] == 'main' ):
                replies.append("%s: %s" % \
                                (self.bold(row['release']), row['version']))
            else:
                replies.append("%s/%s: %s" % \
                                (self.bold(row['release']), row['component'],
                                  row['version']))

        irc.reply( "%s -- %s" % (package, "; ".join(replies)) )

    versions = wrap(versions, ['something', getopts( { 'arch':'something', 'release':'something' } ), optional( 'something' ) ] )

    def names(self, irc, msg, args, package, optlist, something ):
        """<pattern> [--arch <i386>] [--release <lenny>]

        Search package names with * and ? as wildcards.
        The current stable release and i386 are searched by default.
        """
        release,arch = parse_standard_options( optlist, something )

        c = self.psql.cursor(cursor_factory=psycopg2.extras.DictCursor)

        packagesql = package.replace( "*", "%" )
        packagesql = packagesql.replace( "?", "_" )

        if package.startswith('src:'):
            searchsource = True
            packagesql = packagesql[4:]
            sql = r"""SELECT DISTINCT version, source AS package, component
                      FROM sources
                      WHERE source LIKE %(package)s AND
                        release=%(release)s
                      ORDER BY source"""
        else:
            searchsource = False
            sql = r"""SELECT DISTINCT version, package, component
                      FROM packages
                      WHERE package LIKE %(package)s AND
                        (architecture=%(arch)s OR architecture='all') AND
                        release=%(release)s
                      ORDER BY package"""

        c.execute( sql,
                  dict( package=packagesql,
                          arch=arch,
                          release=release ) );
        pkgs=[]
        for row in c.fetchall():
            pkgs.append( row )

        if not pkgs:
            irc.reply( "Sorry, no packages matching '%s' were found." % package )
            return

        replies=[]
        for row in pkgs:
            if( row['component'] == 'main' ):
                replies.append("%s %s" % \
                                (self.bold(row['package']), row['version']) )
            else:
                replies.append("%s %s (%s)" % z
                                (self.bold(row['package']), row['version'],
                                  row['component']) )

        irc.reply( "%s in %s/%s: %s" % (package, release, arch, "; ".join(replies)) )

    names = wrap(names, ['something', getopts( { 'arch':'something', 'release':'something' } ), optional( 'something' ) ] )

    def info(self, irc, msg, args, package, optlist, something ):
        """<packagename> [--arch <i386>] [--release <lenny>]

        Show the short description and some other brief details about a package
        in the specified release and architecture. By default, the current
        stable release and i386 are used.
        """
        release,arch = parse_standard_options( optlist, something )

        c = self.psql.cursor(cursor_factory=psycopg2.extras.DictCursor)
        c.execute(r"""SELECT p.section, p.priority, p.version,
                      p.size, p.installed_size, p.description,
                      p.homepage, s.screenshot_url
                    FROM packages as p
                      LEFT JOIN screenshots as s ON p.package=s.package
                    WHERE p.package=%(package)s AND
                      (p.architecture=%(arch)s OR p.architecture='all') AND
                      p.release=%(release)s""",
                   dict( package=package,
                         arch=arch,
                         release=release) );

        row = c.fetchone()

        if row:
            ds = row['description'].splitlines()
            if ds:
                d = ds[0]
            else:
                d=""
            reply = "%s (%s, %s): %s. Version: %s; Size: %0.1fk; Installed: %dk" % \
                      ( package, row['section'], row['priority'], d,
                        row['version'], row['size']/1024.0, row['installed_size'] )
            if row[6]:    # homepage field
                reply += "; Homepage: %s" % row['homepage']
            if row[7]:    # screenshot url from screenshots.debian.net
                reply += "; Screenshot: %s" % row['screenshot_url']

            irc.reply(reply)
        else:
            irc.reply( "No record of package '%s' in %s/%s." % \
                                    (package, release, arch) )

    info = wrap(info, ['something', getopts( { 'arch':'something',
                                              'release':'something' } ), optional( 'something' ) ] )

    def archHelper(self, irc, msg, args, package, optlist, something ):
        """<packagename> [--release <lenny>]

        Show for what architectures a package is available. By default, the current
        stable release is used.
        """
        release,arch = parse_standard_options( optlist, something )

        c = self.psql.cursor()
        c.execute(r"""SELECT architecture, version
                      FROM packages
                      WHERE package=%(package)s
                        AND release=%(release)s""",
                   dict( package=package,
                         release=release) );
        pkgs=[]
        for row in c.fetchall():
            pkgs.append( [row[0], row[1]] )

        if not pkgs:
            irc.reply( "Sorry, no package named '%s' was found." % package )
            return

        replies=[]
        for row in pkgs:
            replies.append("%s (%s)" % (row[0], row[1]) )

        irc.reply( "%s in %s: %s" % (package, release, "; ".join(replies)) )

    arches = wrap(archHelper, ['something', getopts({ 'release':'something' } ), optional( 'something' ) ] )
    archs  = wrap(archHelper, ['something', getopts({ 'release':'something' } ), optional( 'something' ) ] )

    def rprovidesHelper( self, irc, msg, args, package, optlist, something ):
        """<packagename> [--arch <i386>] [--release <lenny>]

        Show the packages that 'Provide' the specified virtual package
        ('reverse provides').
        By default, the current stable release and i386 are used.
        """
        release,arch = parse_standard_options( optlist, something )

        r = Release(self.psql, arch=arch, release=release)
        p = r.Package(package)

        if p.IsVirtual():
            reply = "%s in %s/%s is provided by: %s." % \
                    ( package, release, arch, ", ".join(p.ProvidersList()) )
            if p.Found():
                reply += " %s is also a real package." % package
        else:
            if p.Found():
                reply = "In %s/%s, %s is a real package." % (release, arch, package)
            else:
                reply = "Sorry, no packages provide '%s' in %s/%s." %\
                                                (package, release, arch)

        irc.reply(reply)

    rprovides = wrap(rprovidesHelper, ['something', getopts( { 'arch':'something',
                                                    'release':'something' } ),
                             optional( 'something' ) ] );
    whatprovides = wrap(rprovidesHelper, ['something', getopts( { 'arch':'something',
                                                    'release':'something' } ),
                             optional( 'something' ) ] );

    def provides(self, irc, msg, args, package, optlist, something ):
        """<packagename> [--arch <i386>] [--release <lenny>]

        Show the list of "provided" packages for the specified binary package
        in the given release and architecture. By default, the current
        stable release and i386 are used.
        """
        release,arch = parse_standard_options( optlist, something )

        r = Release(self.psql, arch=arch, release=release)
        p = r.Package(package)

        if p.Found():
            if p.data['provides']:
                irc.reply("%s in %s/%s provides: %s." % \
                            (package, release, arch, p.data['provides']) )
            else:
                irc.reply("%s in %s/%s provides no additional packages." % \
                            (package, release, arch) )
        else:
            irc.reply("Cannot find the package %s in %s/%s." % \
                            (package, release, arch) )

    provides = wrap(provides, ['something', getopts( { 'arch':'something',
                                              'release':'something' } ), optional( 'something' ) ] )

    def danke( self, irc, msg, args ):
        """
        Someone is trying to speak esperanto to me
        """
        irc.reply( "ne dankinde" )

    danke = wrap( danke, [] )

    def source( self, irc, msg, args, package, optlist, something ):
        """<packagename> [--release <lenny>]

        Show the name of the source package from which a given binary package
        is derived.
        By default, the current stable release and i386 are used.
        """
        release,arch = parse_standard_options( optlist, something )

        r = Release(self.psql, arch=arch, release=release)
        p = r.bin2src(package)
        if p:
            irc.reply( "%s -- Source: %s" % ( package, p ) )
        else:
            irc.reply( "Sorry, there is no record of a source package for the binary package '%s' in %s/%s." % \
                  ( package, release, arch ) )

    src = wrap(source, ['something', getopts( { 'release':'something' } ),
                           optional( 'something' ) ] );

    def binaries( self, irc, msg, args, package, optlist, something ):
        """<packagename> [--release <lenny>]

        Show the name of the binary package(s) that are derived from a given
        source package.
        By default, the current stable release and i386 are used.
        """
        release,arch = parse_standard_options( optlist, something )

        r = Release(self.psql, arch=arch, release=release)
        p = r.Source(package)

        if p.Found():
            reply = "%s -- Binaries: %s" % (package, ", ".join(p.Binaries()))
            irc.reply( reply )
        else:
            irc.reply("Cannot find the package %s in %s/%s." % \
                            (package, release, arch) )

    binaries = wrap(binaries, ['something', getopts( { 'release':'something' } ),
                           optional( 'something' ) ] );

    def builddep( self, irc, msg, args, package, optlist, something ):
        """<packagename> [--release <lenny>]

        Show the name of the binary packages on which a given source package
        or binary package build-depends.
        By default, the current stable release is used.
        """
        release,arch = parse_standard_options( optlist, something )

        def bdformat(package, bd, bdi):
            reply = []
            if bd:
                reply.append("Build-Depends: %s" % bd)
            if bdi:
                reply.append("Build-Depends-Indep: %s" % bdi)
            return "%s -- %s." % ( package, "; ".join(reply))

        r = Release(self.psql, arch=arch, release=release)
        p = r.Source(package)
        if not p.Found():
            irc.reply( "Sorry, there is no record of the %s package in %s." %(package, release) )
            return

        bd = p.RelationEntry('build_depends')
        bdi = p.RelationEntry('build_depends_indep')

        irc.reply(bdformat(package, bd, bdi))

    builddep = wrap(builddep, ['something', getopts( { 'release':'something' } ), optional( 'something' )] );

    def relationshipHelper( self, irc, msg, args, package, optlist, something, relation ):
        """Does the dirty work for each of the functions that show
        "conflicts", "depends", "recommends", "suggests", "enhances".

        The standard usage for each of these functions is accepted:
            relationship <packagename> [--arch <i386>] [--release <lenny>]

        Show the packages that are listed as 'Depends' for a given package.
        By default, the current stable release and i386 are used.
        """
        knownRelations = [ 'conflicts',
                           'depends',
                           'recommends',
                           'suggests',
                           'enhances' ]

        if not relation in knownRelations:
            irc.error("Sorry, unknown error determining package relationships.")

        release,arch = parse_standard_options( optlist, something )

        r = Release(self.psql, arch=arch, release=release)
        p = r.Package(package)

        if p.Found():
            irc.reply( "%s -- %s: %s." % ( package, relation, p.RelationEntry(relation)) )
        else:
            irc.reply( "Sorry, no package named '%s' was found in %s/%s." % \
                                (package, release, arch) )

    def conflicts( self, irc, msg, args, package, optlist, something ):
        """<packagename> [--arch <i386>] [--release <lenny>]

        Show the binary packages listed as conflicting with a given binary
        package.
        By default, the current stable release and i386 are used.
        """
        self.relationshipHelper(irc, msg, args, package, optlist, something, 'conflicts')

    conflicts = wrap(conflicts, ['something', getopts( { 'arch':'something',
                                                         'release':'something' } ),
                                 optional( 'something' )] );

    def depends( self, irc, msg, args, package, optlist, something ):
        """<packagename> [--arch <i386>] [--release <lenny>]

        Show the packages that are listed as 'Depends' for a given package.
        By default, the current stable release and i386 are used.
        """
        self.relationshipHelper(irc, msg, args, package, optlist, something, 'depends')

    depends = wrap(depends, ['something', getopts( { 'arch':'something',
                                                         'release':'something' } ),
                                 optional( 'something' )] );

    def recommends( self, irc, msg, args, package, optlist, something ):
        """<packagename> [--arch <i386>] [--release <lenny>]

        Show the packages that are listed as 'Recommends' for a given package.
        By default, the current stable release and i386 are used.
        """
        self.relationshipHelper(irc, msg, args, package, optlist, something, 'recommends')

    recommends = wrap(recommends, ['something', getopts( { 'arch':'something',
                                                         'release':'something' } ),
                                   optional( 'something' )] );

    def suggests( self, irc, msg, args, package, optlist, something ):
        """<packagename> [--arch <i386>] [--release <lenny>]

        Show the packages that are listed as 'Suggests' for a given package.
        By default, the current stable release and i386 are used.
        """
        self.relationshipHelper(irc, msg, args, package, optlist, something, 'suggests')

    suggests = wrap(suggests, ['something', getopts( { 'arch':'something',
                                                         'release':'something' } ),
                               optional( 'something' ) ] );

    def enhances( self, irc, msg, args, package, optlist, something ):
        """<packagename> [--arch <i386>] [--release <lenny>]

        Show the packages that are listed as 'Enhances' for a given package.
        By default, the current stable release and i386 are used.
        """
        self.relationshipHelper(irc, msg, args, package, optlist, something, 'enhances')

    enhances = wrap(enhances, ['something', getopts( { 'arch':'something',
                                                         'release':'something' } ),
                               optional( 'something' ) ] );

    def checkdeps( self, irc, msg, args, package, optlist, something ):
        """<packagename> [--arch <i386>] [--release <lenny>] [--type depends|recommends|suggests|conflicts]

        Check that the dependencies listed by a package are satisfiable for the
        specified release and architecture.
        By default, all dependency types with the current stable release and
        i386 are used.
        """
        release,arch = parse_standard_options( optlist, something )

        knownRelations = [ 'conflicts',
                           'depends',
                           'recommends',
                           'suggests' ]

        relation = []
        for( option,arg ) in optlist:
            if option=='type':
                if arg in knownRelations:
                    relation.append(arg)
                else:
                    irc.error("Bad relationship specified. Use depends, recommends, suggests or conflicts.")
        if not relation:
            relation = knownRelations

        r = Release(self.psql, arch=arch, release=release)
        relchecker = RelationChecker(r)

        badlist = []
        for rel in relation:
            badrels, goodrels = relchecker.Check(package, rel)
            if badrels:
                rlist = map(lambda r: r['relation'], badrels)
                badlist.append("%s: %s" % (self.bold(rel.title()), ", ".join(rlist)))
            elif badrels == None:
                irc.reply( "Sorry, no package named '%s' was found in %s/%s." % \
                                    (package, release, arch) )
                return

        if badlist:
            irc.reply( "%s in %s/%s unsatisfiable dependencies: %s." % \
                        ( package, release, arch, "; ".join(badlist) ) )
        else:
            irc.reply( "%s in %s/%s: all dependencies satisfied." % \
                        ( package, release, arch) )

    checkdeps = wrap(checkdeps, ['something', getopts( { 'arch':'something',
                                                         'release':'something',
                                                         'type':'something' } ),
                               optional( 'something' ) ] );

    def checkBuildDepsHelper(self, relchecker, source):
        def checkRel(rel, longname):
            bd = source.RelationsEntryList(rel)
            badrels,  goodrels  = relchecker.CheckRelationsList(bd)
            if badrels:
                rlist = map(lambda r: r['relation'], badrels)
                return "%s: %s" % (self.bold(longname), ", ".join(rlist))
            return None

        badlist = [
                checkRel('build_depends', 'Build-Depends'),
                checkRel('build_depends_indep', 'Build-Depends-Indep')
            ]
        return filter(None, badlist)

    def checkbuilddeps( self, irc, msg, args, package, optlist, something ):
        """<packagename> [--release <lenny>] [--arch <i386>]

        Check that the build-dependencies listed by a package are satisfiable for the
        specified release and host architecture.
        By default, the current stable release and i386 are used.
        """
        release,arch = parse_standard_options( optlist, something )

        r = Release(self.psql, arch=arch, release=release)
        relchecker = RelationChecker(r)

        s = r.Source(package)
        if not s.Found():
            irc.reply( "Sorry, no package named '%s' was found in %s." % \
                                (package, release) )
            return

        badlist = self.checkBuildDepsHelper(relchecker, s)
        if badlist:
            irc.reply( "%s in %s/%s unsatisfiable build dependencies: %s." % \
                        ( package, release, arch, "; ".join(badlist) ) )
        else:
            irc.reply( "%s in %s/%s: all build-dependencies satisfied." % \
                        ( package, release, arch) )

    checkbuilddeps = wrap(checkbuilddeps, ['something', getopts( {'arch':'something',
                                                         'release':'something',
                                                         } ),
                               optional( 'something' ) ] );

    def bug( self, irc, msg, args, bugno ):
        """
        Show information about a bug in a given pacage.  
        Usage: "bug bugnumber"
        """
        c = self.psql.cursor()
        c.execute( "SELECT package, status, severity, title, last_modified FROM bugs WHERE id=%(bugno)s limit 1", dict( bugno=bugno ) )

        row = c.fetchone()
        if row:
            ds = row[3].splitlines()
            if ds:
                d = ds[0]
            else:
                d=""
            irc.reply( "Bug #%d (%s) %s -- %s; Severity: %s; Last Modified: %s" % ( bugno, row[1], row[0], d, row[2], row[4]) )

    bug = wrap(bug, ['int'] )

    def popcon( self, irc, msg, args, package ):
        """<packagename>

        Show the popcon (popularity contents) data for a given binary package.
        http://popcon.debian.org/FAQ
        """
        c = self.psql.cursor()

        c.execute( "SELECT insts,vote,olde,recent,nofiles FROM popcon where package=%(package)s", dict( package=package ) )

        row = c.fetchone()
        if row:
            irc.reply( "Popcon data for %s: inst: %d, vote: %d, old: %d, recent: %d, nofiles: %d" % (package, row[0],row[1],row[2],row[3],row[4]) )
        else:
            irc.reply( "no popcon data for %s" % (package) )

    popcon = wrap(popcon, ['something'] )

    def uploaderHelper( self, irc, msg, args, package, version ):
        """<packagename> [<version>]

        Return the names of the person who uploaded the package, the person who
        changed the package prior to upload and the maintainer of the specified
        source package. If version is omitted, the most recent upload is used.
        """
        c = self.psql.cursor()

        if version:
            c.execute(r"""SELECT signed_by_name, changed_by_name,
                            maintainer_name, nmu, version
                          FROM upload_history
                          WHERE source=%(package)s AND version=%(version)s""",
                      dict(package=package, version=version) )
        else:
            c.execute(r"""SELECT signed_by_name, changed_by_name,
                            maintainer_name, nmu, version
                          FROM upload_history
                          WHERE source=%(package)s
                          ORDER BY date DESC LIMIT 1""",
                      dict(package=package) )

        row = c.fetchone()
        if row:
            reply = "Package %s version %s was uploaded by %s, last changed by %s and maintained by %s." % (package, row[4], row[0], row[1], row[2])
            if row[3]:
                reply += " (non-maintainer upload)"

            irc.reply( reply )
        else:
            source = self.bin2src(package, 'sid')
            if source:
                return self.uploaderHelper(irc, msg, args, source, version)
            elif version:
                irc.reply( "Sorry, there is no record of '%s', version '%s'." % (package,version) )
            else:
                irc.reply( "Sorry, there is no record of '%s'." % package )

    uploader   = wrap(uploaderHelper, ['something', optional( 'something' )] )
    changer    = wrap(uploaderHelper, ['something', optional( 'something' )] )
    maint      = wrap(uploaderHelper, ['something', optional( 'something' )] )
    maintainer = wrap(uploaderHelper, ['something', optional( 'something' )] )

    def recentHelper( self, irc, msg, args, package, version ):
        """<packagename>

        Return the dates and versions of recent uploads of the specified source
        or binary package.
        """
        c = self.psql.cursor()

        c.execute(r"""SELECT version, date
                      FROM upload_history
                      WHERE source=%(package)s
                      ORDER BY date DESC LIMIT 10""",
                  dict(package=package) )

        uploads = map(lambda u: "%s %s" % (self.bold(u[0]), u[1].date()), c.fetchall())
        if uploads:
            reply = "Package %s recent uploads: %s." % \
                        (package, ", ".join(uploads))
            irc.reply( reply )
        else:
            source = self.bin2src(package, 'sid')
            if source:
                return self.recentHelper(irc, msg, args, source, version)
            irc.reply( "Sorry, there is no record of source package '%s'." % package )

    recent   = wrap(recentHelper, ['something', optional( 'something' )] )

    def rcbugs( self, irc, msg, args, package ):
        """
        Return the release critical bugs for a given package.
        Usage: "rcbugs packagename"
        """

        c = self.psql.cursor()

        if package.startswith( 'src:' ):
            package = package[4:]
            c.execute( "SELECT id FROM bugs inner join packages on packages.package=bugs.package WHERE packages.source=%(package)s AND severity in ('critical', 'grave', 'serious' ) and status not in ('done,fixed') order by bugs.id", dict( package=package ) )
        else:
            c.execute( "SELECT id FROM bugs WHERE package=%(package)s AND severity in ('critical', 'grave', 'serious' ) and status not in ('done,fixed') order by bugs.id", dict( package=package ) )

        reply = "RC bugs in %s:" % package;
        for row in c.fetchall():
            reply += " %d" % row[0]

        irc.reply( reply )

    rcbugs = wrap(rcbugs, ['something'] )

    def file(self, irc, msg, args, glob, optlist, something):
        """<pattern> [--arch <i386>] [--release <lenny>] [--regex | --exact]

        Returns packages that include files matching <pattern> which, by
        default, is interpreted as a glob (see glob(7)).
        If --regex is given, the pattern is treated as a regex (see regex(7)).
        If --exact is given, the exact filename is required.
        The current stable release and i386 are searched by default.
        """
        # Based on the file command in the Debian plugin by James Vega

        release,arch = parse_standard_options( optlist )

        mode = 'glob'
        for (option, arg) in optlist:
            if option == 'exact':
                mode = 'exact'
            elif option == 'regex' or option == 'regexp':
                mode = 'regex'

        # Convert the glob/re/fixed string to a regexp.
        # Strip leading / since they're not in the index anyway.
        # Contents file is whitespace delimited.
        regexp = glob
        if mode == 'glob':
            regexp = fnmatch.translate(regexp)
            #print regexp
            if regexp.startswith(r'\/'):
                regexp = '^' + regexp[2:]
            elif not regexp.startswith(r'.*'):
                regexp = '.*' + regexp
            if regexp.endswith(r'$'):
                regexp = regexp[:-1] + r'[[:space:]]'
        elif mode == 'regex':
            if regexp.startswith(r'/'):
                regexp = '^' + regexp[1:]
            if regexp.endswith(r'$'):
                regexp = regexp[:-1] + r'[[:space:]]'
        else:
            regexp = regexp =  r'^%s[[:space:]]' % re.escape(regexp.lstrip(r'/'))

        self.log.debug("RE=%s" % regexp)

        packages = self.getContents(irc, release, arch, regexp)

        if len(packages) == 0:
            irc.reply('No packages were found with that file.')
        else:
            s = packages.toString(self.bold)
            irc.reply("%s in %s/%s: %s" % (glob, release, arch, s))

    file = wrap(file, [optional('something'),
                        getopts({'arch':'something',
                                'release':'something',
                                'regex':'',
                                'regexp':'',
                                'exact':''
                                }),
                       optional('something')
                       ])

    def getContents(self, irc, release, arch, regexp):
        """
        Find the packages that provide files matching a particular regexp.
        """
        # Abstracted out to permit substitution with a db etc

        path     = self.registryValue('base_path')
        data     = conf.supybot.directories.data()
        filename = 'debian-%s/Contents-%s.gz' % (release, arch)
        contents = os.path.join(data, path, filename)

        try:
            re_obj = re.compile(regexp, re.I)
        except re.error, e:
            irc.error(format('Error in regexp: %s', e), Raise=True)

        if not os.path.isfile(contents):
            irc.error("Sorry, couldn't look up file list.", Raise=True)

        try:
            #print "Trying: zgrep -ie '%s' '%s'" % (regexp, contents)
            output = subprocess.Popen(['zgrep', '-ie', regexp, contents],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True).communicate()[0]
        except TypeError:
            irc.error(r"Sorry, couldn't look up the file list.", Raise=True)

        packages = PackageFileList()
        try:
            for line in output.split("\n"):
                if len(data) > 100:
                    irc.error('There were more than 100 files matching your search, '
                              'please narrow your search.', Raise=True)
                try:
                    (filename, pkg_list) = line.split()
                    if filename == 'FILE':
                        # This is the last line before the actual files.
                        continue
                except ValueError: # Unpack list of wrong size.
                    continue       # We've not gotten to the files yet.
                packages.add(filename, pkg_list.split(','))
        finally:
            pass
        return packages

    def bold(self, s):
        if self.registryValue('bold', dynamic.channel):
            return ircutils.bold(s)
        return s

Class = Judd

# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
