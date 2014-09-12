#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import hashlib
import os
import posixpath
import socket
import sys
import random
import tempfile
import time
from urlparse import urljoin, urlparse
import uuid

import boto
from gaiatest import GaiaApps, GaiaDevice
from marionette import Marionette
from mozdevice import ADBDevice
import mozfile
from mozlog.structured import formatters, handlers, structuredlog
from mozrunner import B2GDeviceRunner
import mozversion
import requests
from thclient import TreeherderRequest, TreeherderJobCollection


DEVICE_PROPERTIES = {
    'flame': {
        'name': 'Flame Device Image',
        'symbol': 'Flame',
        'input': '/dev/input/event0',
        'dimensions': {'x': 480, 'y': 854},
        'home': {'key': 102}},
    'msm7627a': {
        'name': 'Buri/Hamachi Device Image',
        'symbol': 'Buri/Hamac',
        'input': '/dev/input/event4',
        'dimensions': {'x': 320, 'y': 510},
        'home': {'x': 160, 'y': 510}}}


class B2GMonkey(object):

    def __init__(self):
        self.parser = argparse.ArgumentParser(
            description='Automated monkey testing on Firefox OS')
        self.parser.add_argument(
            '--device',
            dest='device_serial',
            help='serial id of a device to use for adb',
            metavar='SERIAL')
        self.parser.add_argument(
            '--log-level',
            default='info',
            help='log level (default: %(default)s)',
            metavar='LEVEL')
        subparsers = self.parser.add_subparsers(title='Commands')
        generate = subparsers.add_parser(
            'generate',
            help='generate an orangutan script for firefox os')
        generate.add_argument(
            'script',
            help='path to write generated orangutan script file')
        generate.add_argument(
            '--seed',
            default=random.random(),
            help='seed to use (defaults to random)')
        generate.add_argument(
            '--steps',
            default=100000,
            help='number of steps',
            metavar='STEPS',
            type=int)
        generate.set_defaults(func=self.generate_script)
        run = subparsers.add_parser(
            'run',
            help='run an orangutan script on firefox os')
        run.add_argument(
            'script',
            help='path to orangutan script file')
        run.add_argument(
            '--address',
            default='localhost:2828',
            help='address of marionette server (default: %(default)s)')
        run.add_argument(
            '--symbols',
            help='path or url containing breakpad symbols')
        run.add_argument(
            '--treeherder',
            default='https://treeherder.mozilla.org/',
            help='location of treeherder instance (default: %(default)s',
            metavar='URL')
        run.set_defaults(func=self.run_script)

    def run(self, args=sys.argv[1:]):
        args = self.parser.parse_args()
        self.logger = structuredlog.StructuredLogger('b2gmonkey')
        handler = handlers.StreamHandler(
            sys.stdout, formatters.JSONFormatter())
        self.logger.add_handler(handlers.LogLevelFilter(
            handler, args.log_level))

        self.version = mozversion.get_version(
            dm_type='adb', device_serial=args.device_serial)
        for k, v in self.version.items():
            self.logger.debug('%s: %s' % (k, v))

        try:
            self.device_properties = DEVICE_PROPERTIES[
                self.version['device_id']]
        except KeyError:
            raise Exception('Firefox OS device not found')

        if not DEVICE_PROPERTIES.get(self.version['device_id']):
            raise Exception('Unsupported device: \'%s\'' %
                            self.version['device_id'])

        self.temp_dir = tempfile.mkdtemp()
        args.func(args)
        mozfile.remove(self.temp_dir)

    def generate_script(self, args):
        self.logger.info('Current seed is: %s' % args.seed)
        rnd = random.Random(str(args.seed))
        dimensions = self.device_properties['dimensions']
        steps = []
        self.logger.info('Generating script with %d steps' % args.steps)
        for i in range(1, args.steps + 1):
            if i % 1000 == 0:
                duration = 2000
                home = self.device_properties['home']
                if 'key' in home:
                    steps.append(['keydown', home['key']])
                    steps.append(['sleep', duration])
                    steps.append(['keyup', home['key']])
                else:
                    steps.append(['tap', home['x'], home['y'], 1, duration])
                continue

            valid_actions = ['tap', 'drag']
            if i == 1 or not steps[-1][0] == 'sleep':
                valid_actions.append('sleep')
            action = rnd.choice(valid_actions)
            if action == 'tap':
                steps.append([
                    action,
                    rnd.randint(1, dimensions['x']),
                    rnd.randint(1, dimensions['y']),
                    rnd.randint(1, 3),  # repetitions
                    rnd.randint(50, 1000)])  # duration
            elif action == 'sleep':
                steps.append([
                    action,
                    rnd.randint(100, 3000)])  # duration
            else:
                steps.append([
                    action,
                    rnd.randint(1, dimensions['x']),  # start
                    rnd.randint(1, dimensions['y']),  # start
                    rnd.randint(1, dimensions['x']),  # end
                    rnd.randint(1, dimensions['y']),  # end
                    rnd.randint(10, 20),  # steps
                    rnd.randint(10, 350)])  # duration

        with open(args.script, 'w+') as f:
            for step in steps:
                f.write(' '.join([str(x) for x in step]) + '\n')
        self.logger.info('Script written to: %s' % args.script)

    def run_script(self, args):
        try:
            host, port = args.address.split(':')
        except ValueError:
            raise ValueError('--address must be in the format host:port')

        # Check that Orangutan is installed
        adb_device = ADBDevice(args.device_serial)
        orng_path = posixpath.join('data', 'local', 'orng')
        if not adb_device.exists(orng_path):
            raise Exception('Orangutan not found! Please install it according '
                            'to the documentation.')

        # TODO: Bug 1038868 - Remove this when Marionette uses B2GDeviceRunner
        runner = B2GDeviceRunner(
            serial=args.device_serial,
            process_args={'stream': None},
            symbols_path=args.symbols)

        runner.start()
        port = runner.device.setup_port_forwarding(remote_port=port)
        assert runner.device.wait_for_port(port), 'Timed out waiting for port!'

        marionette = Marionette(host=host, port=port)
        marionette.start_session()

        # Prepare device
        gaia_device = GaiaDevice(marionette)
        gaia_device.wait_for_b2g_ready()
        gaia_device.unlock()
        gaia_apps = GaiaApps(marionette)
        gaia_apps.kill_all()

        # TODO: Disable bluetooth, emergency calls, carrier, etc

        # Run Orangutan script
        remote_script = posixpath.join(adb_device.test_root, 'orng.script')
        adb_device.push(args.script, remote_script)
        self.start_time = time.time()
        adb_device.shell('%s %s %s' % (orng_path,
                                       self.device_properties['input'],
                                       remote_script))
        self.end_time = time.time()
        adb_device.rm(remote_script)

        # Check for crashes
        # TODO: Detect crash (requires mozrunner release)
        self.crashed = runner.check_for_crashes()
        # TODO: Collect any crash dumps

        # Collect logcat
        logcat = os.path.join(self.temp_dir, 'logcat.txt')
        with open(logcat, mode='w+') as f:
            f.writelines(adb_device.get_logcat())
        self.logger.info('Logcat stored in: %s' % logcat)

        # Report results to Treeherder
        required_envs = ['TREEHERDER_KEY', 'TREEHERDER_SECRET']
        if all([os.environ.get(v) for v in required_envs]):
            self.treeherder_url = args.treeherder
            self.artifacts = [args.script, logcat]
            self.post_to_treeherder()
        else:
            self.logger.info(
                'Results will not be posted to Treeherder. Please set the '
                'following environment variables to enable Treeherder '
                'reports: %s' % ', '.join([
                    v for v in required_envs if not os.environ.get(v)]))

        # Remove logcat
        mozfile.remove(logcat)

    def post_to_treeherder(self):
        job_collection = TreeherderJobCollection()
        job = job_collection.get_job()
        job.add_group_name(self.device_properties['name'])
        job.add_group_symbol(self.device_properties['symbol'])
        job.add_job_name('Orangutan Monkey Script (%s)' %
                         self.version['device_id'])
        job.add_job_symbol('Om')

        # Determine revision hash from application revision
        revision = self.version['application_changeset']
        project = self.version['application_repository'].split('/')[-1]
        lookup_url = urljoin(self.treeherder_url,
                             'api/project/%s/revision-lookup/?revision=%s' % (
                                 project, revision))
        self.logger.debug('Getting revision hash from: %s' % lookup_url)
        response = requests.get(lookup_url)
        response.raise_for_status()
        assert response.json(), 'Unable to determine revision hash for %s. ' \
                                'Perhaps it has not been ingested by ' \
                                'Treeherder?' % revision
        revision_hash = response.json()[revision]['revision_hash']
        job.add_revision_hash(revision_hash)
        job.add_project(project)
        job.add_job_guid(str(uuid.uuid4()))
        job.add_product_name('b2g')
        job.add_state('completed')
        job.add_result(self.crashed and 'testfailed' or 'success')

        job.add_submit_timestamp(int(self.start_time))
        job.add_start_timestamp(int(self.start_time))
        job.add_end_timestamp(int(self.end_time))

        job.add_machine(socket.gethostname())
        job.add_build_info('b2g', 'b2g-device-image', 'x86')
        job.add_machine_info('b2g', 'b2g-device-image', 'x86')

        # All B2G device builds are currently opt builds
        job.add_option_collection({'opt': True})

        date_format = '%d %b %Y %H:%M:%S'
        job_details = [{
            'content_type': 'link',
            'title': 'Gaia revision:',
            'url': 'https://github.com/mozilla-b2g/gaia/commit/%s' %
                   self.version.get('gaia_changeset'),
            'value': self.version.get('gaia_changeset'),
        }, {
            'content_type': 'text',
            'title': 'Gaia date:',
            'value': self.version.get('gaia_date') and time.strftime(
                date_format, time.localtime(int(
                    self.version.get('gaia_date')))),
        }, {
            'content_type': 'text',
            'title': 'Device identifier:',
            'value': self.version.get('device_id')
        }, {
            'content_type': 'text',
            'title': 'Device firmware (date):',
            'value': self.version.get('device_firmware_date') and
                     time.strftime(date_format, time.localtime(int(
                         self.version.get('device_firmware_date')))),
        }, {
            'content_type': 'text',
            'title': 'Device firmware (incremental):',
            'value': self.version.get('device_firmware_version_incremental')
        }, {
            'content_type': 'text',
            'title': 'Device firmware (release):',
            'value': self.version.get('device_firmware_version_release')
        }]

        ci_url = os.environ.get('BUILD_URL')
        if ci_url:
            job_details.append({
                'url': ci_url,
                'value': ci_url,
                'content_type': 'link',
                'title': 'CI build:'})

        required_envs = ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY']
        upload_artifacts = all([os.environ.get(v) for v in required_envs])

        if upload_artifacts:
            conn = boto.connect_s3()
            bucket = conn.create_bucket(
                os.environ.get('S3_UPLOAD_BUCKET', 'b2gmonkey'))
            bucket.set_acl('public-read')

            for artifact in self.artifacts:
                if artifact and os.path.exists(artifact):
                    h = hashlib.sha512()
                    with open(artifact, 'rb') as f:
                        for chunk in iter(lambda: f.read(1024 ** 2), b''):
                            h.update(chunk)
                    _key = h.hexdigest()
                    key = bucket.get_key(_key)
                    if not key:
                        key = bucket.new_key(_key)
                    key.set_contents_from_filename(artifact)
                    key.set_acl('public-read')
                    blob_url = key.generate_url(expires_in=0,
                                                query_auth=False)
                    job_details.append({
                        'url': blob_url,
                        'value': os.path.split(artifact)[-1],
                        'content_type': 'link',
                        'title': 'Artifact:'})
                    self.logger.info('Artifact %s uploaded to: %s' % (
                        artifact, blob_url))
        else:
            self.logger.info(
                'Artifacts will not be included with the report. Please '
                'set the following environment variables to enable '
                'uploading of artifacts: %s' % ', '.join([
                    v for v in required_envs if not os.environ.get(v)]))

        if job_details:
            job.add_artifact('Job Info', 'json', {'job_details': job_details})

        job_collection.add(job)

        # Send the collection to Treeherder
        url = urlparse(self.treeherder_url)
        request = TreeherderRequest(
            protocol=url.scheme,
            host=url.hostname,
            project=project,
            oauth_key=os.environ.get('TREEHERDER_KEY'),
            oauth_secret=os.environ.get('TREEHERDER_SECRET'))
        self.logger.info('Sending results to Treeherder: %s' %
                         self.treeherder_url)
        self.logger.debug('Job collection: %s' %
                          job_collection.to_json())
        response = request.post(job_collection)
        self.logger.debug('Response: %s' % response.read())
        assert response.status == 200, 'Failed to send results!'
        self.logger.info('Results are available to view at: %s' % (
            urljoin(self.treeherder_url, '/ui/#/jobs?repo=%s&revision=%s' % (
                project, revision))))


def cli(args=sys.argv[1:]):
    b2gmonkey = B2GMonkey()
    b2gmonkey.run(args)

if __name__ == '__main__':
    cli()
