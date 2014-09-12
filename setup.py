# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from setuptools import setup

setup(name='b2gmonkey',
      version='1.0',
      description='Automated monkey testing for Firefox OS',
      long_description=open('README.md').read(),
      keywords='mozilla automation monkey testing firefox os',
      author='Dave Hunt',
      author_email='dhunt@mozilla.com',
      url='https://github.com/davehunt/b2gmonkey',
      license='Mozilla Public License 2.0 (MPL 2.0)',
      entry_points={
          'console_scripts': [
              'b2gmonkey = b2gmonkey:cli']},
      install_requires=[
          'boto>=2.32.1',
          'gaiatest>=0.28',
          'mozdevice>=0.40',
          'mozcrash>=0.13',
          'mozlog>=2.5',
          'mozrunner>=6.3',
          'mozversion>=0.6',
          'requests>=2.3.0',
          'treeherder-client>=1.0'])
