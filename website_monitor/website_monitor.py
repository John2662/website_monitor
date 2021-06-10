# -*- coding: utf-8 -*-

"""Main module."""
import copy
import datetime
import getopt
import json
import logging as log
import os
import re as reg_ex
import sys
import threading
import time

import requests

from website_monitor import db_utils
from .wm_exceptions import (
    ConfigFileEmpty, ConfigFileInvalid, RequirementsNotFulfilled,
    URLPropertyNotFound
)

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(WORK_DIR, 'logs')
LOG_FILE_PATH = os.path.join(LOG_DIR, 'logfile.log')

log.basicConfig(filename=LOG_FILE_PATH, format='%(message)s', level=log.INFO)


DEFAULT_CHECK_PERIOD = 3600 

class WebMonitorConfigObject(object):
    """Represents a configuration object."""

    def __init__(self, check_period=0, defer_to_store=False, config_abs_path=None):
        """
        Initialize a WebMonitorConfigObject instance.

        :param config_abs_path: String representing absolute path
            to configuration file
        :param check_period: Value representing the interval period between
            website status checks.
        """
        config_path = config_abs_path or os.path.join(WORK_DIR, 'config.json')
        with open(config_path) as f:
            configs = json.load(f)
        self.set_check_period_and_web_data(configs, check_period, defer_to_store)

    # check if website properties have at least defined the url
    # if they are properly formed, add them in the set of
    # self.websites
    def extract_websites(self, configs):
        self.websites = copy.copy(configs)
        for key, val in configs.items():
            if 'url' not in val:
                self.websites.pop(key,None)

    @staticmethod
    def is_positive_int(i):
        try:
            i = int(i)
        except:
            return False
        return i > 0

    @staticmethod
    def extract_check_period_from_input(test_check_period):
        try:
            test_check_period = int(test_check_period)
        except:
            test_check_period = 0
        return test_check_period

    def set_check_period_and_web_data(self, configs, passed_check_period, defer_to_store):
        passed_check_period = self.__class__.extract_check_period_from_input(passed_check_period)
        stored_check_period = self.__class__.extract_check_period_from_input(configs.pop('check_period', 0))
        self.extract_websites(configs)
        if defer_to_store and stored_check_period > 0:
            self.check_period = stored_check_period
        elif passed_check_period > 0:
            self.check_period = passed_check_period
        else:
            self.check_period = DEFAULT_CHECK_PERIOD

    @property
    def check_period(self):
        return self.__check_period

    @check_period.setter
    def check_period(self, val):
        try:
            val = int(val)
        except ValueError:
            print('Please make sure that check period value is specified '
                 'as integer.')
            return False
        if val < 0:
            print('Checking period cannot be negative. Please set correct '
                 'value and try again.')
            return False
        self.__check_period = val

class Monitor(object):
    """Represents Monitor object."""
    config_obj = None

    def __init__(self, check_interval):
        """
        Initialize a Monitor instance.

        :param config_obj: website_monitor.WebMonitorConfigObject class instance
        """
        self.config_store = WebMonitorConfigObject(check_interval, False)
        self.load_website_query_table()

        self.next_call = time.time()
        self.start_watch()

    def hot_load_config(self):
        defer_to_json_configs = True
        self.config_store = WebMonitorConfigObject(self.config_store.check_period, defer_to_json_configs)


    def load_website_query_table(self):
        for webname, web_data in self.config_store.websites.items():
            url = web_data['url']
            content_requirements = web_data.get('content', None)
            print(f'{webname}, {url}, {content_requirements}')
            db_utils.insert_webcheck_config(webname, url, content_requirements)

    def start_watch(self):
        """
        Method responsible for triggering periodic checks in time intervals.
        If time interval is not specified it is set by default to 3600s(1h).

        :return: None
        """
        self.hot_load_config()
        self._start_checks()
        self.next_call += self.config_store.check_period
        # accounts for drift
        # more at https://stackoverflow.com/a/18180189/2808371
        threading.Timer(self.next_call - time.time(), self.start_watch).start()

    def _start_checks(self):
        """
        Method responsible for coordinating checks of each website.

        :return: None
        """
        # used for formatting first and last message of round of checks
        time_format = '%d/%m/%Y %H:%M:%S'
        asterix = '*' * 10
        s = ('\n{asterix}Starting new round of checks - {current_time}'
             '{asterix}')
        log.info(s.format(asterix=asterix,
                          current_time=datetime.datetime.now().strftime(
                              time_format)))

        threads = []
        for webname, web_data in self.config_store.websites.items():
            url = web_data['url']
            content_requirements = web_data.get('content', None)
            t = threading.Thread(target=self._perform_checks, args=(
                url, content_requirements, webname))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()
        s = '\n{asterix}Finished all checks - {current_time}{asterix}'
        log.info(s.format(asterix=asterix,
                          current_time=datetime.datetime.now().strftime(
                              time_format)))

    def _perform_checks(self, url, content_requirements, webname):
        """
        Method responsible for checking requirements on each website.

        :param url: URL of the page for which we want to check requirements
        :param content_requirements: Actual content requirements
        :return: None
        """
        response = self.make_request(url, webname)

        if not response:
            return
        response_time = response.elapsed / datetime.timedelta(seconds=1)
        requirements_fulfilled = 1
        try:
            self.check_requirements(response, content_requirements)
        except RequirementsNotFulfilled as e:
            s = ('Content requirements: {e} ("{content_requirements}" '
                 'not in response content)')
            log.info(s.format(**locals()))
            requirements_fulfilled = 0
        else:
            s = ('Content requirements: Website meets content requirements.'
                 '("{content_requirements}" in response content)')
            log.info(s.format(**locals()))

        db_utils.insert_webcheck_record(webname, url, datetime.datetime.now(),
                              response.status_code, response_time, requirements_fulfilled)

    @staticmethod
    def make_request(url, webname=None):
        """
        Static method used to perform actual request to the server.

        :param url: URL of the page that we want to make request to
        :param webname: Alias name for website
        :return: If successful returns requests.Response object, otherwise None
        """
        try:
            response = requests.get(url)
        except requests.exceptions.RequestException as e:
            error_msg = str(e)
            s = 'Connection problem\nError message: {}\n'
            log.info(s.format(error_msg))
            db_utils.insert_webcheck_record(
                webname, url, request_time=datetime.datetime.now(),
                error=error_msg)
        else:
            s = ('\nURL: {url}\nStatus: {response.status_code}\n'
                 'Response time: {response.elapsed.seconds}s'
                 '{response.elapsed.microseconds}\u00B5s')
            log.info(s.format(**locals()))
            return response
        return None

    @staticmethod
    def check_requirements(response, content_requirements):
        """
        Static method used to perform requirement checks for specific
        requests.Response.

        :param response: requests.Response object.
        :param content_requirements: Content requirements to check against
            in response object.
        :return: If requirements are met returns True, otherwise raises
            website_monitor.exceptions.RequirementsNotFulfilled
        """
        response_content = response.content.decode('utf-8', 'ignore')
        requirements_are_met = reg_ex.search(content_requirements,
                                         response_content, reg_ex.IGNORECASE)

        if not content_requirements or requirements_are_met:
            # if there are no requirements or the requirements are fulfilled
            return True
        s = 'Website content does not match specified requirements.'
        raise RequirementsNotFulfilled(s.format(**locals()))


def parse_cl_args(argv):
    """
    Helper function used to check if user provided checking period value
    in command line arguments.

    :param argv: command line arguments
    :return: checking period value
    """
    help_text = """
    Usage:
        website_monitor.py -i <checking_interval_in_s>
        website_monitor.py --interval=<checking_interval_in_s>
    """
    try:
        opts, args = getopt.getopt(argv, "hi:", ["help", "interval="])
    except getopt.GetoptError:
        print(help_text)
        sys.exit(2)
    for opt, val in opts:
        if opt == '-h':
            print(help_text)
            sys.exit(0)
        elif opt in ("-i", "--interval"):
            return val

def main():
    interval = parse_cl_args(sys.argv[1:])
    db_utils.create_tables()
    Monitor(interval)

if __name__ == '__main__':
    main()
