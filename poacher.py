import sys
import os
import stat
import shutil
import json
import time
import argparse
from git import Repo
from git import GitCommandError
from optparse import OptionParser
from github import Github

banner = """
::::::::::.     ...       :::.       .,-:::::    ::   .:  .,::::::  :::::::..
 `;;;```.;;; .;;;;;;;.    ;;`;;    ,;;;'````'   ,;;   ;;, ;;;;''''  ;;;;``;;;;
  `]]nnn]]' ,[[     \[[, ,[[ '[[,  [[[         ,[[[,,,[[[  [[cccc    [[[,/[[['
   $$$''    $$$,     $$$c$$$cc$$$c $$$         '$$$'''$$$  $$''''    $$$$$$c
   888o     '888,_ _,88P 888   888,`88bo,__,o,  888   '88o 888oo,__  888b '88bo,
   YMMMb      'YMMMMMP'  YMM   ''`   'YUMMMMMP' MMM    YMM '''YUMMM MMMM   'W'
"""

sys.path.append('utils')
import marker
import tlog

parser = argparse.ArgumentParser()
parser.add_argument('-v', '--verbose', action='store_true', dest='verbose')
args = parser.parse_args()

MAIN_CONF = 'conf/poacher.json'
conf = {
    'skip_empty_repos' : True,
    'max_repo_size_kb' : 20000,
    'clone'            : False,
    'monitor_only'     : True,
}

DEFAULT_STEP =          16
UPPER_INIT =            64

CONF_REQUIRED = [
    'archive_directory', 'working_directory', 'github_username',
    'github_password'
]

def check_main_conf(conf):
    ret = True
    for item in CONF_REQUIRED:
        if item not in conf or conf[item].strip() == '':
            tlog.log('Error: please set %s in file %s' % (item, MAIN_CONF))
            ret = False

    return ret

def parse_user_handler(abspath):
    mod_dir, mod = os.path.split(abspath)
    return mod_dir, os.path.splitext(mod)[0]

def import_user_handler(conf):
    if conf['monitor_only']:
        conf['clone'] = False
        return None, None

    # Import handler module from main conf, if defined...
    repo_handler = None
    module_name = None

    if 'repo_handler' in conf and conf['repo_handler'] != "":
        module_dir, module_name = parse_user_handler(conf['repo_handler'])
        repo_handler_logger = lambda msg: tlog.log(msg, desc=module_name)

        sys.path.append(module_dir)

        try:
            repo_handler = __import__(module_name)
        except Exception as e:
            tlog.log("Error importing handler %s: %s" % (module_name, e))

    return module_name, repo_handler

try:
    with open(MAIN_CONF, 'r') as fh:
        conf.update(json.load(fh))
except Exception as e:
    tlog.log('Error reading file %s: %s' % (MAIN_CONF, e))
    sys.exit(1)

if not check_main_conf(conf):
    sys.exit(1)

if args.verbose:
    print banner

module_name, repo_handler = import_user_handler(conf)
marker = marker.Marker()

def authenticate():
    return Github(conf['github_username'], conf['github_password'])

def get_new(githubObj, last):
    ret = []

    for repo in githubObj.get_repos(since=last):
            ret.append(repo)

    return ret

def repo_exists(githubObj, id):
    try:
        githubObj.get_repos(since=id)[0]
    except:
        return False
    else:
        return True

def predict_growth(ts, sumavg, numsessions):
    avg = float(sumavg) / float(numsessions)
    delta_s = time.time() - ts
    delta_m = float(delta_s) / 60.0
    return int(delta_m * avg)

def bsearch(githubObj, lower, growth_prediction):
    upper = lower + growth_prediction
    set = None

    tlog.log('Starting binary search for latest repo ID, last ID was %s'
             % lower)

    step = DEFAULT_STEP
    while not set:
        tlog.log('trying ID %s' % upper)

        if repo_exists(githubObj, upper):
            upper += step
            step *= 2
        else:
            tlog.log('ID %s not yet used\n' % upper)
            set = 1

    tlog.log('Beginning search between %s and %s'
             % (lower, upper))

    while (lower + 1) < upper:
        tlog.log('search area size: %s' % (upper - lower))
        middle = lower + (upper - lower) / 2

        if repo_exists(githubObj, middle):
            lower = middle
        else:
            upper = middle

    return lower

def del_rw(action, name, exc):
    if os.path.exists(name):
        os.chmod(name, stat.S_IWRITE)

        desc = 'file or directory'
        try:
            if os.path.isdir(name):
                desc = 'directory'
                shutil.rmtree(name)
            else:
                desc = 'file'
                os.remove(name)
        except:
            tlog.log("Failed to remove %s %s, abandoning..." %
                     (desc, name))

def archive(archive_dir, repo, handler_logs):
    if not os.path.isdir(conf['archive_directory']):
        os.mkdir(conf['archive_directory'])

    path = os.path.join(conf['archive_directory'],
        os.path.basename(archive_dir) +  '_' + str(repo.id))

    os.mkdir(path)
    infofile = os.path.join(path, 'info.txt')
    with open(infofile, 'w') as fh:
        fh.write('URL : %s\n' % repo.html_url.encode('utf-8'))
        fh.write('created at : %s\n' %
            repo.created_at.strftime('%m/%d/%Y %H:%M:%S'))
        if len(handler_logs) > 0:
            fh.write('\nlogs:\n\n')
            for log in handler_logs:
                fh.write('%s\n' % log.encode('utf-8'))

    try:
        shutil.copytree(archive_dir, path + '/' + os.path.basename(archive_dir))
    except:
        tlog.log(("Failed to copy repo files while archiving: leaving "
                 "in %s") % conf['working_directory'])
        return

    shutil.rmtree(archive_dir, onerror=del_rw)

def run_handler(current, repo, handler_log):
    max_retries = 2
    retries = 0

    while retries < max_retries:
        try:
            ret = repo_handler.run(current, repo, handler_log)
        except Exception as e:
            tlog.log("Error in handler: %s" % e)
            retries = retries + 1
            time.sleep(1)
        else:
            return ret

    tlog.log("Max. retries reach. Unable to process repo " + repo.full_name)
    return False

def get_repo_size(repo):
    try:
        size = repo.size
    except:
        tlog.log("Repo %s has unknown size" % repo.name)
        return 0, 0

    mbsize = size
    try:
        mbsize = float(size) / 1000.0
    except:
        tlog.log("Repo %s has unknown size" % repo.name)
        return 0, 0

    return size, mbsize

def clone_repo(repo, size, mbsize, conf):
    if not conf['clone']:
        return None

    if size > conf['max_repo_size_kb']:
        tlog.log('%.2fMB: Repo is too big, will not clone' % mbsize)
        return None

    current = conf['working_directory'] + '/' + repo.name
    clone_path = current.decode()

    tlog.log('Cloning %s' % repo.name)
    try:
        Repo.clone_from(repo.html_url, current)
    except GitCommandError as e:
        tlog.log('Unable to clone: %s: skipping...' % e)
        return None

    # Sleep for 100ms to ensure files have finished downloading
    time.sleep(0.1)
    return clone_path

def main_loop():
    tlog.init(args.verbose)
    G = authenticate()

    if repo_handler != None:
        tlog.log('Using handler %s' % module_name)

    guess = 0
    if marker.averages_sum > 0 and marker.numsessions > 0:
        guess = predict_growth(marker.timestamp, marker.averages_sum,
                marker.numsessions)

    newest = bsearch(G, marker.repo_id, guess)
    marker.current_id = newest
    marker.starting_id = newest
    marker.starttime = time.time()

    tlog.log('Latest repo ID is %d' % newest)

    if not os.path.isdir(conf['working_directory']):
        os.mkdir(conf['working_directory'])

    while True:
        try:
            new = get_new(G, newest)
        except Exception as e:
            tlog.log(str(e))
            sys.exit(1)

        numnew = len(new)
        if numnew == 0:
            continue

        newest = marker.newest_id = new[-1].id
        marker.numrepos += numnew
        marker.current_timestamp = time.time()

        for repo in new:
            marker.current_id = repo.id

            if float(repo.size) == 0.0000 and conf['skip_empty_repos']:
                continue

            size, mbsize = get_repo_size(repo)
            tlog.log('%s (%.2fMB)' % (repo.html_url, mbsize))

            if repo_handler == None:
                continue

            clone_path = clone_repo(repo, size, mbsize, conf)
            handler_logs = []

            def handler_log(msg):
                handler_logs.append(msg)
                tlog.log(msg, desc=module_name)

            if run_handler(clone_path, repo, handler_log) and conf["clone"]:
                archive(clone_path, repo, handler_logs)
            elif conf["clone"] and clone_path != None:
                shutil.rmtree(clone_path, onerror=del_rw)

def get_avg_per_min(delta, num):
    per_sec = float(num) / float(delta)
    return int(per_sec * 60)

def finish():
    npub = marker.numrepos
    nall = marker.newest_id - marker.starting_id

    delta = marker.current_timestamp - marker.starttime
    deltastr = tlog.secs_to_walltime(delta, compact=False)

    avg = get_avg_per_min(delta, nall)
    marker.save(avg)

    r_avg = float(marker.averages_sum + avg) / float(marker.numsessions + 1)
    tlog.log('%d new repos (%d public) in %s.' % (nall, npub, deltastr))
    tlog.log('Session average: %d new repos per minute' % avg)
    tlog.log('Running average: %d new repos per minute.' % r_avg)
    sys.exit(0)


def main():
    if ('repo_handler' not in conf or conf['repo_handler'] == ""
            or repo_handler == None):
        tlog.log("Monitor Mode (no active handler. keeping track of repository "
                 "creation rate, nothing more)")

    try:
        main_loop()
    except KeyboardInterrupt:
        tlog.write('Finishing...')
        time.sleep(2)
        finish()

if __name__ == "__main__":
    main()
