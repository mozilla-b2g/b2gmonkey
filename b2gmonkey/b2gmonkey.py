#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import gzip
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
from marionette import MarionetteException
from mozdevice import ADBDevice
import mozfile
import mozlog
from mozlog import structured
from mozlog.structured.formatters import TbplFormatter
from mozlog.structured.handlers import LogLevelFilter, StreamHandler
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
    'flame-kk': {
        'name': 'Flame KitKat Device Image',
        'symbol': 'Flame-KK'},
    'msm7627a': {
        'name': 'Buri/Hamachi Device Image',
        'symbol': 'Buri/Hamac',
        'input': '/dev/input/event4',
        'dimensions': {'x': 320, 'y': 510},
        'home': {'x': 160, 'y': 510}}}


class B2GMonkeyError(Exception):
    pass


class S3UploadError(Exception):

    def __init__(self):
        Exception.__init__(self, 'Error uploading to S3')


class B2GMonkey(object):

    def __init__(self, device_serial=None):
        self.device_serial = device_serial

        self._logger = structured.get_default_logger(component='b2gmonkey')
        if not self._logger:
            self._logger = mozlog.getLogger('b2gmonkey')

        self.version = mozversion.get_version(
            dm_type='adb', device_serial=device_serial)

        device_id = self.version.get('device_id')
        if not device_id:
            raise B2GMonkeyError('Firefox OS device not found.')

        self.device_properties = DEVICE_PROPERTIES.get(device_id)
        if not self.device_properties:
            raise B2GMonkeyError('Unsupported device: \'%s\'' % device_id)

        android_version = self.version.get('device_firmware_version_release')
        if device_id == 'flame' and android_version == '4.4.2':
            self.device_properties.update(DEVICE_PROPERTIES.get('flame-kk'))

        self.temp_dir = tempfile.mkdtemp()
        self.crash_dumps_path = os.path.join(self.temp_dir, 'crashes')
        os.mkdir(self.crash_dumps_path)
        os.environ['MINIDUMP_SAVE_PATH'] = self.crash_dumps_path

    def __del__(self):
        if hasattr(self, 'temp_dir') and os.path.exists(self.temp_dir):
            mozfile.remove(self.temp_dir)

    def generate(self, script, seed=None, steps=10000, **kwargs):
        seed = seed or random.random()
        self._logger.info('Current seed is: %s' % seed)
        rnd = random.Random(str(seed))
        dimensions = self.device_properties['dimensions']
        _steps = []
        self._logger.info('Generating script with %d steps' % steps)
        for i in range(1, steps + 1):
            if i % 1000 == 0:
                duration = 2000
                home = self.device_properties['home']
                if 'key' in home:
                    _steps.append(['keydown', home['key']])
                    _steps.append(['sleep', duration])
                    _steps.append(['keyup', home['key']])
                else:
                    _steps.append(['tap', home['x'], home['y'], 1, duration])
                continue

            valid_actions = ['tap', 'drag']
            if i == 1 or not _steps[-1][0] == 'sleep':
                valid_actions.append('sleep')
            action = rnd.choice(valid_actions)
            if action == 'tap':
                _steps.append([
                    action,
                    rnd.randint(1, dimensions['x']),
                    rnd.randint(1, dimensions['y']),
                    rnd.randint(1, 3),  # repetitions
                    rnd.randint(50, 1000)])  # duration
            elif action == 'sleep':
                _steps.append([
                    action,
                    rnd.randint(100, 3000)])  # duration
            else:
                _steps.append([
                    action,
                    rnd.randint(1, dimensions['x']),  # start
                    rnd.randint(1, dimensions['y']),  # start
                    rnd.randint(1, dimensions['x']),  # end
                    rnd.randint(1, dimensions['y']),  # end
                    rnd.randint(10, 20),  # steps
                    rnd.randint(10, 350)])  # duration

        with open(script, 'w+') as f:
            for step in _steps:
                f.write(' '.join([str(x) for x in step]) + '\n')
        self._logger.info('Script written to: %s' % script)

    def run(self, script, address='localhost:2828', symbols=None,
            treeherder='https://treeherder.mozilla.org/', reset=False,
            **kwargs):
        try:
            host, port = address.split(':')
        except ValueError:
            raise ValueError('--address must be in the format host:port')

        # Check that Orangutan is installed
        self.adb_device = ADBDevice(self.device_serial)
        orng_path = posixpath.join('data', 'local', 'orng')
        if not self.adb_device.exists(orng_path):
            raise Exception('Orangutan not found! Please install it according '
                            'to the documentation.')

        self.runner = B2GDeviceRunner(
            serial=self.device_serial,
            process_args={'stream': None},
            symbols_path=symbols,
            logdir=self.temp_dir)

        if reset:
            self.runner.start()
        else:
            self.runner.device.connect()

        port = self.runner.device.setup_port_forwarding(remote_port=port)
        assert self.runner.device.wait_for_port(port), \
            'Timed out waiting for port!'

        marionette = Marionette(host=host, port=port)
        marionette.start_session()

        try:
            marionette.set_context(marionette.CONTEXT_CHROME)
            self.is_debug = marionette.execute_script(
                'return Components.classes["@mozilla.org/xpcom/debug;1"].'
                'getService(Components.interfaces.nsIDebug2).isDebugBuild;')
            marionette.set_context(marionette.CONTEXT_CONTENT)

            if reset:
                gaia_device = GaiaDevice(marionette)
                gaia_device.wait_for_b2g_ready(timeout=120)
                gaia_device.unlock()
                gaia_apps = GaiaApps(marionette)
                gaia_apps.kill_all()

            # TODO: Disable bluetooth, emergency calls, carrier, etc

            # Run Orangutan script
            remote_script = posixpath.join(self.adb_device.test_root,
                                           'orng.script')
            self.adb_device.push(script, remote_script)
            self.start_time = time.time()
            # TODO: Kill remote process on keyboard interrupt
            self.adb_device.shell('%s %s %s' % (orng_path,
                                                self.device_properties['input'],
                                                remote_script))
            self.end_time = time.time()
            self.adb_device.rm(remote_script)
        except (MarionetteException, IOError):
            if self.runner.crashed:
                # Crash has been detected
                pass
            else:
                raise
        self.runner.check_for_crashes(test_name='b2gmonkey')

        # Report results to Treeherder
        required_envs = ['TREEHERDER_KEY', 'TREEHERDER_SECRET']
        if all([os.environ.get(v) for v in required_envs]):
            self.post_to_treeherder(script, treeherder)
        else:
            self._logger.info(
                'Results will not be posted to Treeherder. Please set the '
                'following environment variables to enable Treeherder '
                'reports: %s' % ', '.join([
                    v for v in required_envs if not os.environ.get(v)]))

    def post_to_treeherder(self, script, treeherder_url):
        job_collection = TreeherderJobCollection()
        job = job_collection.get_job()
        job.add_group_name(self.device_properties['name'])
        job.add_group_symbol(self.device_properties['symbol'])
        job.add_job_name('Orangutan Monkey Script (%s)' %
                         self.device_properties.get('symbol'))
        job.add_job_symbol('Om')

        # Determine revision hash from application revision
        revision = self.version['application_changeset']
        project = self.version['application_repository'].split('/')[-1]
        lookup_url = urljoin(treeherder_url,
                             'api/project/%s/revision-lookup/?revision=%s' % (
                                 project, revision))
        self._logger.debug('Getting revision hash from: %s' % lookup_url)
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
        job.add_result(self.runner.crashed and 'testfailed' or 'success')

        job.add_submit_timestamp(int(self.start_time))
        job.add_start_timestamp(int(self.start_time))
        job.add_end_timestamp(int(self.end_time))

        job.add_machine(socket.gethostname())
        job.add_build_info('b2g', 'b2g-device-image', 'x86')
        job.add_machine_info('b2g', 'b2g-device-image', 'x86')

        if self.is_debug:
            job.add_option_collection({'debug': True})
        else:
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

        # Attach log files
        handlers = [handler for handler in self._logger.handlers
                    if isinstance(handler, StreamHandler) and
                    os.path.exists(handler.stream.name)]
        for handler in handlers:
            path = handler.stream.name
            filename = os.path.split(path)[-1]
            try:
                url = self.upload_to_s3(path)
                job_details.append({
                    'url': url,
                    'value': filename,
                    'content_type': 'link',
                    'title': 'Log:'})
                # Add log reference
                if type(handler.formatter) is TbplFormatter or \
                        type(handler.formatter) is LogLevelFilter and \
                        type(handler.formatter.inner) is TbplFormatter:
                    job.add_log_reference(filename, url)
            except S3UploadError:
                job_details.append({
                    'value': 'Failed to upload %s' % filename,
                    'content_type': 'text',
                    'title': 'Error:'})

        # Attach script
        filename = os.path.split(script)[-1]
        try:
            url = self.upload_to_s3(script)
            job_details.append({
                'url': url,
                'value': filename,
                'content_type': 'link',
                'title': 'Script:'})
        except S3UploadError:
            job_details.append({
                'value': 'Failed to upload %s' % filename,
                'content_type': 'text',
                'title': 'Error:'})

        # Attach logcat
        filename = '%s.log' % self.runner.device.dm._deviceSerial
        path = os.path.join(self.temp_dir, filename)
        try:
            url = self.upload_to_s3(path)
            job_details.append({
                'url': url,
                'value': filename,
                'content_type': 'link',
                'title': 'Logcat:'})
        except S3UploadError:
            job_details.append({
                'value': 'Failed to upload %s' % filename,
                'content_type': 'text',
                'title': 'Error:'})

        if job_details:
            job.add_artifact('Job Info', 'json', {'job_details': job_details})

        # Attach crash dumps
        if self.runner.crashed:
            crash_dumps = os.listdir(self.crash_dumps_path)
            for filename in crash_dumps:
                path = os.path.join(self.crash_dumps_path, filename)
                try:
                    url = self.upload_to_s3(path)
                    job_details.append({
                        'url': url,
                        'value': filename,
                        'content_type': 'link',
                        'title': 'Crash:'})
                except S3UploadError:
                    job_details.append({
                        'value': 'Failed to upload %s' % filename,
                        'content_type': 'text',
                        'title': 'Error:'})

        job_collection.add(job)

        # Send the collection to Treeherder
        url = urlparse(treeherder_url)
        request = TreeherderRequest(
            protocol=url.scheme,
            host=url.hostname,
            project=project,
            oauth_key=os.environ.get('TREEHERDER_KEY'),
            oauth_secret=os.environ.get('TREEHERDER_SECRET'))
        self._logger.info('Sending results to Treeherder: %s' % treeherder_url)
        self._logger.debug('Job collection: %s' %
                           job_collection.to_json())
        response = request.post(job_collection)
        if response.status == 200:
            self._logger.debug('Response: %s' % response.read())
            self._logger.info('Results are available to view at: %s' % (
                urljoin(treeherder_url, '/ui/#/jobs?repo=%s&revision=%s' % (
                    project, revision))))
        else:
            self._logger.error('Failed to send results to Treeherder! '
                               'Response: %s' % response.read())

    def upload_to_s3(self, path):
        if not hasattr(self, '_s3_bucket'):
            try:
                self._logger.debug('Connecting to S3')
                conn = boto.connect_s3()
                bucket = os.environ.get('S3_UPLOAD_BUCKET', 'b2gmonkey')
                if conn.lookup(bucket):
                    self._logger.debug('Getting bucket: %s' % bucket)
                    self._s3_bucket = conn.get_bucket(bucket)
                else:
                    self._logger.debug('Creating bucket: %s' % bucket)
                    self._s3_bucket = conn.create_bucket(bucket)
                self._s3_bucket.set_acl('public-read')
            except boto.exception.NoAuthHandlerFound:
                self._logger.info(
                    'Please set the following environment variables to enable '
                    'uploading of artifacts: %s' % ', '.join([v for v in [
                        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'] if not
                        os.environ.get(v)]))
                raise S3UploadError()
            except boto.exception.S3ResponseError as e:
                self._logger.warning('Upload to S3 failed: %s' % e.message)
                raise S3UploadError()

        h = hashlib.sha512()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 ** 2), b''):
                h.update(chunk)
        _key = h.hexdigest()
        key = self._s3_bucket.get_key(_key)
        if not key:
            self._logger.debug('Creating key: %s' % _key)
            key = self._s3_bucket.new_key(_key)
        ext = os.path.splitext(path)[-1]
        if ext == '.log':
            key.set_metadata('Content-Type', 'text/plain')

        with tempfile.NamedTemporaryFile('w+b', suffix=ext) as tf:
            self._logger.debug('Compressing: %s' % path)
            with gzip.GzipFile(path, 'wb', fileobj=tf) as gz:
                with open(path, 'rb') as f:
                    gz.writelines(f)
            tf.flush()
            tf.seek(0)
            key.set_metadata('Content-Encoding', 'gzip')
            self._logger.debug('Setting key contents from: %s' % tf.name)
            key.set_contents_from_filename(tf.name)

        key.set_acl('public-read')
        blob_url = key.generate_url(expires_in=0,
                                    query_auth=False)
        self._logger.info('File %s uploaded to: %s' % (path, blob_url))
        return blob_url


def cli(args=sys.argv[1:]):
    parser = argparse.ArgumentParser(
        description='Automated monkey testing on Firefox OS')
    parser.add_argument(
        '--device',
        dest='device_serial',
        help='serial id of a device to use for adb',
        metavar='SERIAL')
    subparsers = parser.add_subparsers(title='Commands')
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
    generate.set_defaults(func='generate')
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
        help='location of treeherder instance (default: %(default)s)',
        metavar='URL')
    run.add_argument(
        '--reset',
        action='store_true',
        default=False,
        help='reset the target to a clean state')
    run.set_defaults(func='run')
    structured.commandline.add_logging_group(parser)

    args = parser.parse_args()
    structured.commandline.setup_logging(
        'b2gmonkey', args, {'mach': sys.stdout})

    b2gmonkey = B2GMonkey(device_serial=args.device_serial)
    getattr(b2gmonkey, args.func)(**vars(args))


if __name__ == '__main__':
    cli()
