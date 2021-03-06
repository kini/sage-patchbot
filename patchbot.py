#!/usr/bin/env python

####################################################################
#
# This is the main script for the patchbot. It pulls patches from
# trac, applies them, and publishes the results of the tests to a
# server running serve.py.  Configuration is primarily done via an
# optional conf.txt file passed in as a command line argument.
#
#          Author: Robert Bradshaw <robertwb@gmail.com>
#
#               Copyright 2010-11 (C) Google, Inc.
#
#  Distributed under the terms of the GNU General Public License (GPL)
#  as published by the Free Software Foundation; either version 2 of
#  the License, or (at your option) any later version.
#                  http://www.gnu.org/licenses/
####################################################################


import signal
import getpass, platform
import random, re, os, shutil, sys, subprocess, time, traceback
import bz2, urllib2, urllib, json
from optparse import OptionParser

from http_post_file import post_multipart

from trac import scrape, pull_from_trac
from util import now_str as datetime, parse_datetime, prune_pending, do_or_die, get_base, compare_version, current_reports

def filter_on_authors(tickets, authors):
    if authors is not None:
        authors = set(authors)
    for ticket in tickets:
        if authors is None or set(ticket['authors']).issubset(authors):
            yield ticket

def contains_any(key, values):
    clauses = [{'key': value} for value in values]
    return {'$or': clauses}

def no_unicode(s):
    return s.encode('ascii', 'replace').replace(u'\ufffd', '?')

def get_ticket(server, return_all=False, **conf):
    query = "raw&status=open&todo"
    if 'trusted_authors' in conf:
        query += "&authors=" + urllib.quote_plus(no_unicode(':'.join(conf['trusted_authors'])), safe=':')
    try:
        handle = urllib2.urlopen(server + "/ticket/?" + query)
        all = json.load(handle)
        handle.close()
    except:
        traceback.print_exc()
        return
    if 'trusted_authors' in conf:
        all = filter_on_authors(all, conf['trusted_authors'])
    all = filter(lambda x: x[0], ((rate_ticket(t, **conf), t) for t in all))
    all.sort()
    if return_all:
        return all
    if all:
        return all[-1]

def lookup_ticket(server, id):
    url = server + "/ticket/?" + urllib.urlencode({'raw': True, 'query': json.dumps({'id': id})})
    res = json.load(urllib2.urlopen(url))
    if res:
        return res[0]
    else:
        return scrape(id)

def compare_machines(a, b, machine_match=None):
    if isinstance(a, dict) or isinstance(b, dict):
        # old format, remove
        return (1,)
    else:
        if machine_match is not None:
            a = a[:machine_match]
            b = b[:machine_match]
        diff = [x != y for x, y in zip(a, b)]
        if len(a) != len(b):
            diff.append(1)
        return diff

def rate_ticket(ticket, **conf):
    rating = 0
    if ticket['spkgs']:
        return # can't handle these yet
    elif not ticket['patches']:
        return # nothing to do
    for dep in ticket['depends_on']:
        if isinstance(dep, basestring) and '.' in dep:
            if compare_version(conf['base'], dep) < 0:
                # Depends on a newer version of Sage than we're running.
                return None
    for author in ticket['authors']:
        if author not in conf['trusted_authors']:
            return
        rating += conf['bonus'].get(author, 0)
    for participant in ticket['participants']:
        rating += conf['bonus'].get(participant, 0) # doubled for authors
    rating += len(ticket['participants'])
    # TODO: remove condition
    if 'component' in ticket:
        rating += conf['bonus'].get(ticket['component'], 0)
    rating += conf['bonus'].get(ticket['status'], 0)
    rating += conf['bonus'].get(ticket['priority'], 0)
    rating += conf['bonus'].get(str(ticket['id']), 0)
    redundancy = (100,)
    prune_pending(ticket)
    if not ticket.get('retry'):
        for reports in current_reports(ticket, base=conf['base']):
            redundancy = min(redundancy, compare_machines(reports['machine'], conf['machine'], conf['machine_match']))
    if not redundancy[-1]:
        return # already did this one
    return redundancy, rating, -int(ticket['id'])

def report_ticket(server, ticket, status, base, machine, user, log, plugins=[]):
    print ticket['id'], status
    report = {
        'status': status,
        'patches': ticket['patches'],
        'deps': ticket['depends_on'],
        'spkgs': ticket['spkgs'],
        'base': base,
        'user': user,
        'machine': machine,
        'time': datetime(),
        'plugins': plugins,
    }
    fields = {'report': json.dumps(report)}
    if status != 'Pending':
        files = [('log', 'log', bz2.compress(open(log).read()))]
    else:
        files = []
    print post_multipart("%s/report/%s" % (server, ticket['id']), fields, files)
    
class TimeOut(Exception):
    pass

def alarm_handler(signum, frame):
    raise Alarm

class Tee:
    def __init__(self, filepath, time=False, timeout=60*60*24):
        self.filepath = filepath
        self.time = time
        self.timeout = timeout
        
    def __enter__(self):
        self._saved = os.dup(sys.stdout.fileno()), os.dup(sys.stderr.fileno())
        self.tee = subprocess.Popen(["tee", self.filepath], stdin=subprocess.PIPE)
        os.dup2(self.tee.stdin.fileno(), sys.stdout.fileno())
        os.dup2(self.tee.stdin.fileno(), sys.stderr.fileno())
        if self.time:
            print datetime()
            self.start_time = time.time()
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            traceback.print_exc()
        if self.time:
            print datetime()
            print int(time.time() - self.start_time), "seconds"
        self.tee.stdin.close()
        time.sleep(1)
        os.dup2(self._saved[0], sys.stdout.fileno())
        os.dup2(self._saved[1], sys.stderr.fileno())
        time.sleep(1)
        try:
            signal.signal(signal.SIGALRM, alarm_handler)
            signal.alarm(self.timeout)
            self.tee.wait()
            signal.alarm(0)
        except TimeOut:
            traceback.print_exc()
            raise
        return False


class Timer:
    def __init__(self):
        self._starts = {}
        self._history = []
        self.start()
    def start(self, label=None):
        self._last_activity = self._starts[label] = time.time()
    def finish(self, label=None):
        try:
            elapsed = time.time() - self._starts[label]
        except KeyError:
            elapsed = time.time() - self._last_activity
        self._last_activity = time.time()
        self.print_time(label, elapsed)
        self._history.append((label, elapsed))
    def print_time(self, label, elapsed):
        print label, '--', int(elapsed), 'seconds'
    def print_all(self):
        for label, elapsed in self._history:
            self.print_time(label, elapsed)

# The sage test scripts could really use some cleanup...
all_test_dirs = ["doc/common", "doc/en", "doc/fr", "sage"]

status = {
    'started': 'ApplyFailed',
    'applied': 'BuildFailed',
    'built'  : 'TestsFailed',
    'tested' : 'TestsPassed',
    'failed_plugin' : 'PluginFailed',
}

def plugin_boundary(name, end=False):
    if end:
        name = 'end ' + name
    return ' '.join(('='*10, name, '='*10))


def test_a_ticket(sage_root, server, ticket=None, nodocs=False):
    base = get_base(sage_root)
    if ticket is None:
        ticket = get_ticket(base=base, server=server, **conf)
    else:
        ticket = None, scrape(int(ticket))
    if not ticket:
        print "No more tickets."
        if random.random() < 0.01:
            cleanup(sage_root, server)
        time.sleep(conf['idle'])
        return
    rating, ticket = ticket
    print "\n" * 2
    print "=" * 30, ticket['id'], "=" * 30
    print ticket['title']
    print "score", rating
    print "\n" * 2
    log_dir = sage_root + "/logs"
    if not os.path.exists(log_dir):
        os.mkdir(log_dir)
    log = '%s/%s-log.txt' % (log_dir, ticket['id'])
    report_ticket(server, ticket, status='Pending', base=base, machine=conf['machine'], user=conf['user'], log=None)
    plugins_results = []
    try:
        with Tee(log, time=True, timeout=conf['timeout']):
            t = Timer()
            start_time = time.time()

            state = 'started'
            os.environ['MAKE'] = "make -j%s" % conf['parallelism']
            os.environ['SAGE_ROOT'] = sage_root
            # TODO: Ensure that sage-main is pristine.
            pull_from_trac(sage_root, ticket['id'], force=True)
            t.finish("Apply")
            state = 'applied'
            
            do_or_die('$SAGE_ROOT/sage -b %s' % ticket['id'])
            t.finish("Build")
            state = 'built'
            
            working_dir = "%s/devel/sage-%s" % (sage_root, ticket['id'])
            # Only the ones on this ticket.
            patches = os.popen2('hg --cwd %s qapplied' % working_dir)[1].read().strip().split('\n')[-len(ticket['patches']):]
            kwds = {
                "original_dir": "%s/devel/sage-0" % sage_root,
                "patched_dir": working_dir,
                "patches": ["%s/devel/sage-%s/.hg/patches/%s" % (sage_root, ticket['id'], p) for p in patches if p],
            }
            for name, plugin in conf['plugins']:
                try:
                    print plugin_boundary(name)
                    plugin(ticket, **kwds)
                    passed = True
                except Exception:
                    traceback.print_exc()
                    passed = False
                finally:
                    t.finish(name)
                    print plugin_boundary(name, end=True)
                    plugins_results.append((name, passed))
                    
            test_dirs = ["$SAGE_ROOT/devel/sage-%s/%s" % (ticket['id'], dir) for dir in all_test_dirs]
            if conf['parallelism'] > 1:
                test_cmd = "-tp %s" % conf['parallelism']
            else:
                test_cmd = "-t"
            do_or_die("$SAGE_ROOT/sage %s -sagenb %s" % (test_cmd, ' '.join(test_dirs)))
            #do_or_die("$SAGE_ROOT/sage -t $SAGE_ROOT/devel/sage-%s/sage/rings/integer.pyx" % ticket['id'])
            #do_or_die('sage -testall')
            t.finish("Tests")
            state = 'tested'
            
            if not all(passed for name, passed in plugins_results):
                state = 'failed_plugin'

            print
            t.print_all()
    except Exception:
        traceback.print_exc()
    
    for _ in range(5):
        try:
            print "Reporting", ticket['id'], status[state]
            report_ticket(server, ticket, status=status[state], base=base, machine=conf['machine'], user=conf['user'], log=log, plugins=plugins_results)
            print "Done reporting", ticket['id']
            break
        except urllib2.HTTPError:
            traceback.print_exc()
            time.sleep(conf['idle'])
    else:
        print "Error reporting", ticket['id']
    return status[state]

def cleanup(sage_root, server):
    print "Looking up closed tickets."
    closed_list = urllib2.urlopen(server + "?status=closed").read()
    closed = set(m.groups()[0] for m in re.finditer(r"/ticket/(\d+)/", closed_list))
    for branch in os.listdir(os.path.join(sage_root, "devel")):
        if branch[:5] == "sage-":
            if branch[5:] in closed:
                to_delete = os.path.join(sage_root, "devel", branch)
                print "Deleting closed ticket:", to_delete
                shutil.rmtree(to_delete)
    print "Done cleaning up."

def default_trusted_authors(server):
    handle = urllib2.urlopen(server + "/trusted/")
    try:
        return json.load(handle).keys()
    finally:
        handle.close()

def machine_data():
    system, node, release, version, arch = os.uname()
    if system.lower() == "linux":
        dist_name, dist_version, dist_id = platform.linux_distribution()
        if dist_name:
            return [dist_name, dist_version, arch, release, node]
    return [system, arch, release, node]

def parse_time_of_day(s):
    def parse_interval(ss):
        ss = ss.strip()
        if '-' in ss:
            start, end = ss.split('-')
            return float(start), float(end)
        else:
            return float(ss), float(ss) + 1
    return [parse_interval(ss) for ss in s.split(',')]

def check_time_of_day(hours):
    from datetime import datetime
    now = datetime.now()
    hour = now.hour + now.minute / 60.
    for start, end in parse_time_of_day(hours):
        if start < end:
            if start <= hour <= end:
                return True
        elif hour <= end or start <= hour:
            return True
    return False

def get_conf(path, server, **overrides):
    if path is None:
        unicode_conf = {}
    else:
        unicode_conf = json.load(open(path))
    # defaults
    conf = {
        "idle": 300,
        "time_of_day": "0-0", # midnight-midnight
        "parallelism": 3,
        "timeout": 3 * 60 * 60,
        "plugins": ["plugins.commit_messages",
                    "plugins.coverage",
                    "plugins.trailing_whitespace",
#                    "plugins.docbuild"
                    ],
        "bonus": {},
        "machine": machine_data(),
        "machine_match": 3,
        "user": getpass.getuser(),
    }
    default_bonus = {
        "needs_review": 1000,
        "positive_review": 500,
        "blocker": 100,
        "critical": 50,
    }
    for key, value in unicode_conf.items():
        conf[str(key)] = value
    for key, value in default_bonus.items():
        if key not in conf['bonus']:
            conf['bonus'][key] = value
    conf.update(overrides)
    if "trusted_authors" not in conf:
        conf["trusted_authors"] = default_trusted_authors(server)
    
    def locate_plugin(name):
        ix = name.rindex('.')
        module = name[:ix]
        name = name[ix+1:]
        plugin = getattr(__import__(module, fromlist=[name]), name)
        assert callable(plugin)
        return plugin
    conf["plugins"] = [(name, locate_plugin(name)) for name in conf["plugins"]]
    return conf

def main(args):
    global conf

    # Most configuration is done in the config file, which is reread between
    # each ticket for live configuration of the patchbot.
    parser = OptionParser()
    parser.add_option("--config", dest="config")
    parser.add_option("--sage-root", dest="sage_root", default=os.environ.get('SAGE_ROOT'))
    parser.add_option("--server", dest="server", default="http://patchbot.sagemath.org/")
    parser.add_option("--count", dest="count", default=1000000)
    parser.add_option("--ticket", dest="ticket", default=None)
    parser.add_option("--list", dest="list", default=False)
    parser.add_option("--skip-base", dest="skip_base", default=False)
    (options, args) = parser.parse_args(args)
    
    conf_path = options.config and os.path.abspath(options.config)
    if options.ticket:
        tickets = [int(t) for t in options.ticket.split(',')]
        count = len(tickets)
    else:
        tickets = None
        count = int(options.count)
    
    conf = get_conf(conf_path, options.server)
    if options.list:
        for score, ticket in get_ticket(base=get_base(options.sage_root), server=options.server, return_all=True, **conf):
            print score, ticket['id'], ticket['title']
            print ticket
            print
        sys.exit(0)

    print "WARNING: Assuming sage-main is pristine."
    if options.sage_root == os.environ.get('SAGE_ROOT'):
        print "WARNING: Do not use this copy of sage while the patchbot is running."

    if not options.skip_base:
        clean = lookup_ticket(options.server, 0)
        def good(report):
            return report['machine'] == conf['machine'] and report['status'] == 'TestsPassed'
        if not any(good(report) for report in current_reports(clean, base=get_base(options.sage_root))):
            res = test_a_ticket(ticket=0, sage_root=options.sage_root, server=options.server)
            if res != 'TestsPassed':
                print "\n\n"
                while True:
                    print "Failing tests in your install: %s. Continue anyways? [y/N] " % res
                    ans = sys.stdin.readline().lower().strip()
                    if ans == '' or ans[0] == 'n':
                        sys.exit(1)
                    elif ans[0] == 'y':
                        break

    for _ in range(count):
        try:
            if tickets:
                ticket = tickets.pop(0)
            else:
                ticket = None
            conf = get_conf(conf_path, options.server)
            if check_time_of_day(conf['time_of_day']):
                test_a_ticket(ticket=ticket, sage_root=options.sage_root, server=options.server)
            else:
                print "Idle."
                time.sleep(conf['idle'])
        except urllib2.HTTPError:
                traceback.print_exc()
                time.sleep(conf['idle'])

if __name__ == '__main__':
    # allow this script to serve as a single entry point for bots and the server
    args = list(sys.argv)
    if len(args) > 1 and args[1] == '--serve':
        del args[1]
        from serve import main
    main(args)
