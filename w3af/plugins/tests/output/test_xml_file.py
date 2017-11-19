# -*- coding: utf8 -*-
"""
test_xml_file.py

Copyright 2012 Andres Riancho

This file is part of w3af, http://w3af.org/ .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
"""
import os
import base64
import os.path
import StringIO
import unittest
import xml.etree.ElementTree as ElementTree

from lxml import etree
from nose.plugins.attrib import attr

import w3af.core.data.constants.severity as severity
import w3af.core.data.kb.knowledge_base as kb

from w3af import ROOT_PATH

from w3af.core.controllers.w3afCore import w3afCore
from w3af.core.controllers.misc.temp_dir import create_temp_dir, remove_temp_dir
from w3af.core.controllers.ci.moth import get_moth_http
from w3af.core.data.kb.tests.test_vuln import MockVuln
from w3af.core.data.kb.vuln import Vuln
from w3af.core.data.db.history import HistoryItem
from w3af.core.data.dc.headers import Headers
from w3af.core.data.parsers.doc.url import URL
from w3af.core.data.url.HTTPResponse import HTTPResponse
from w3af.core.data.url.HTTPRequest import HTTPRequest
from w3af.core.data.options.option_list import OptionList
from w3af.core.data.options.opt_factory import opt_factory
from w3af.core.data.options.option_types import OUTPUT_FILE
from w3af.plugins.tests.helper import PluginTest, PluginConfig, MockResponse
from w3af.plugins.output.xml_file import xml_file
from w3af.plugins.output.xml_file import HTTPTransaction, ScanInfo, Finding


@attr('smoke')
class TestXMLOutput(PluginTest):

    target_url = get_moth_http('/audit/sql_injection/where_integer_qs.py')

    FILENAME = 'output-unittest.xml'
    XSD = os.path.join(ROOT_PATH, 'plugins', 'output', 'xml_file', 'report.xsd')

    _run_configs = {
        'cfg': {
            'target': target_url + '?id=3',
            'plugins': {
                'audit': (PluginConfig('sqli'),),
                'output': (
                    PluginConfig(
                        'xml_file',
                        ('output_file', FILENAME, PluginConfig.STR)),
                )
            },
        }
    }

    def test_found_vuln(self):
        cfg = self._run_configs['cfg']
        self._scan(cfg['target'], cfg['plugins'])

        kb_vulns = self.kb.get('sqli', 'sqli')
        file_vulns = get_vulns_from_xml(self.FILENAME)

        self.assertEqual(len(kb_vulns), 1, kb_vulns)

        self.assertEquals(
            set(sorted([v.get_url() for v in kb_vulns])),
            set(sorted([v.get_url() for v in file_vulns]))
        )

        self.assertEquals(
            set(sorted([v.get_name() for v in kb_vulns])),
            set(sorted([v.get_name() for v in file_vulns]))
        )

        self.assertEquals(
            set(sorted([v.get_plugin_name() for v in kb_vulns])),
            set(sorted([v.get_plugin_name() for v in file_vulns]))
        )

        self.assertEqual(validate_xml(file(self.FILENAME).read(), self.XSD),
                         '')

    def tearDown(self):
        super(TestXMLOutput, self).tearDown()
        try:
            os.remove(self.FILENAME)
        except:
            pass
        finally:
            self.kb.cleanup()

    def test_error_null_byte(self):
        # https://github.com/andresriancho/w3af/issues/12924
        plugin_instance = xml_file()
        plugin_instance.error('\0')
        plugin_instance.flush()


class TestNoDuplicate(unittest.TestCase):
    
    FILENAME = 'output-unittest.xml'
    
    def setUp(self):
        kb.kb.cleanup()
        create_temp_dir()
        HistoryItem().init()

    def tearDown(self):
        remove_temp_dir()
        HistoryItem().clear()
        kb.kb.cleanup()

    def test_no_duplicate_vuln_reports(self):
        # The xml_file plugin had a bug where vulnerabilities were written to
        # disk multiple times, this test makes sure I fixed that vulnerability

        # Write the HTTP request / response to the DB
        url = URL('http://w3af.com/a/b/c.php')
        hdr = Headers([('User-Agent', 'w3af')])
        request = HTTPRequest(url, data='a=1')
        request.set_headers(hdr)

        hdr = Headers([('Content-Type', 'text/html')])
        res = HTTPResponse(200, '<html>syntax error near', hdr, url, url)

        _id = 1

        h1 = HistoryItem()
        h1.request = request
        res.set_id(_id)
        h1.response = res
        h1.save()

        # Create one vulnerability in the KB pointing to the request-
        # response we just created
        desc = 'Just a test for the XML file output plugin.'
        v = Vuln('SQL injection', desc, severity.HIGH, _id, 'sqli')
        kb.kb.append('sqli', 'sqli', v)

        self.assertEqual(len(kb.kb.get_all_vulns()), 1)

        # Setup the plugin
        plugin_instance = xml_file()

        # Set the output file for the unittest
        ol = OptionList()
        d = 'Output file name where to write the XML data'
        o = opt_factory('output_file', self.FILENAME, d, OUTPUT_FILE)
        ol.add(o)

        # Then we flush() twice to disk, this reproduced the issue
        plugin_instance.set_options(ol)
        plugin_instance.flush()
        plugin_instance.flush()
        plugin_instance.flush()

        # Now we parse the vulnerabilities from disk and confirm only one
        # is there
        file_vulns = get_vulns_from_xml(self.FILENAME)
        self.assertEqual(len(file_vulns), 1, file_vulns)


class XMLParser(object):

    def __init__(self):
        self.vulns = []
        self._inside_body = False
        self._inside_response = False
        self._data_parts = []
    
    def start(self, tag, attrib):
        """
        <vulnerability id="[87]" method="GET"
                       name="Cross site scripting vulnerability"
                       plugin="xss" severity="Medium"
                       url="http://moth/w3af/audit/xss/simple_xss_no_script_2.php"
                       var="text">
        """
        if tag == 'vulnerability':
            name = attrib['name']
            plugin = attrib['plugin']
            
            v = MockVuln(name, None, 'High', 1, plugin)
            v.set_url(URL(attrib['url']))
            
            self.vulns.append(v)
        
        # <body content-encoding="base64">
        elif tag == 'body':
            content_encoding = attrib['content-encoding']
            
            assert content_encoding == 'base64'
            self._inside_body = True

        elif tag == 'http-response':
            self._inside_response = True
    
    def end(self, tag):
        if tag == 'body' and self._inside_response:
            
            data = ''.join(self._data_parts)

            data_decoded = base64.b64decode(data)
            assert 'syntax error' in data_decoded, data_decoded
            assert 'near' in data_decoded, data_decoded
            
            self._inside_body = False
            self._data_parts = []

        if tag == 'http-response':
            self._inside_response = False

    def data(self, data):
        if self._inside_body and self._inside_response:
            self._data_parts.append(data)

    def close(self):
        return self.vulns


def get_vulns_from_xml(filename):
    xp = XMLParser()
    parser = etree.XMLParser(target=xp)
    vulns = etree.fromstring(file(filename).read(), parser)
    return vulns


def validate_xml(content, schema_content):
    """
    Validate an XML against an XSD.

    :return: The validation error log as a string, an empty string is returned
             when there are no errors.
    """
    xml_schema_doc = etree.parse(schema_content)
    xml_schema = etree.XMLSchema(xml_schema_doc)
    xml = etree.parse(StringIO.StringIO(content))

    # Validate the content against the schema.
    try:
        xml_schema.assertValid(xml)
    except etree.DocumentInvalid:
        return xml_schema.error_log

    return ''


class TestXMLOutputBinary(PluginTest):

    target_url = 'http://rpm-path-binary/'

    TEST_FILE = os.path.join(ROOT_PATH, 'plugins', 'tests', 'output',
                             'data', 'nsepa32.rpm')

    MOCK_RESPONSES = [
              MockResponse(url='http://rpm-path-binary/',
                           body=file(TEST_FILE).read(),
                           content_type='text/plain',
                           method='GET', status=200),
    ]

    FILENAME = 'output-unittest.xml'

    _run_configs = {
        'cfg': {
            'target': target_url,
            'plugins': {
                'grep': (PluginConfig('path_disclosure'),),
                'output': (
                    PluginConfig(
                        'xml_file',
                        ('output_file', FILENAME, PluginConfig.STR)),
                )
            },
        }
    }

    def test_binary_handling_in_xml(self):
        cfg = self._run_configs['cfg']
        self._scan(cfg['target'], cfg['plugins'])

        self.assertEquals(len(self.kb.get_all_findings()), 1)

        try:
            tree = ElementTree.parse(self.FILENAME)
            tree.getroot()
        except Exception, e:
            self.assertTrue(False, 'Generated invalid XML: "%s"' % e)

    def tearDown(self):
        super(TestXMLOutputBinary, self).tearDown()
        try:
            os.remove(self.FILENAME)
        except:
            pass
        finally:
            self.kb.cleanup()


class TestXML0x0B(PluginTest):

    target_url = 'http://0x0b-path-binary/'

    TEST_FILE = os.path.join(ROOT_PATH, 'plugins', 'tests', 'output',
                             'data', '0x0b.html')

    MOCK_RESPONSES = [
              MockResponse(url='http://0x0b-path-binary/',
                           body=file(TEST_FILE).read(),
                           content_type='text/plain',
                           method='GET', status=200),
    ]

    FILENAME = 'output-unittest.xml'

    _run_configs = {
        'cfg': {
            'target': target_url,
            'plugins': {
                'grep': (PluginConfig('path_disclosure'),),
                'output': (
                    PluginConfig(
                        'xml_file',
                        ('output_file', FILENAME, PluginConfig.STR)),
                )
            },
        }
    }

    def test_binary_0x0b_handling_in_xml(self):
        cfg = self._run_configs['cfg']
        self._scan(cfg['target'], cfg['plugins'])

        self.assertEquals(len(self.kb.get_all_findings()), 1)

        try:
            tree = ElementTree.parse(self.FILENAME)
            tree.getroot()
        except Exception, e:
            self.assertTrue(False, 'Generated invalid XML: "%s"' % e)

    def tearDown(self):
        super(TestXML0x0B, self).tearDown()
        try:
            os.remove(self.FILENAME)
        except:
            pass
        finally:
            self.kb.cleanup()


class TestSpecialCharacterInURL(PluginTest):

    target_url = u'http://hello.se/%C3%93%C3%B6'

    MOCK_RESPONSES = [
              MockResponse(url=target_url,
                           body=u'hi there á! /var/www/site/x.php path',
                           content_type='text/plain',
                           method='GET', status=200),
    ]

    FILENAME = 'output-unittest.xml'

    _run_configs = {
        'cfg': {
            'target': target_url,
            'plugins': {
                'grep': (PluginConfig('path_disclosure'),),
                'output': (
                    PluginConfig(
                        'xml_file',
                        ('output_file', FILENAME, PluginConfig.STR)),
                )
            },
        }
    }

    def test_special_character_in_url_handling(self):
        cfg = self._run_configs['cfg']
        self._scan(cfg['target'], cfg['plugins'])

        self.assertEquals(len(self.kb.get_all_findings()), 1)

        try:
            tree = ElementTree.parse(self.FILENAME)
            tree.getroot()
        except Exception, e:
            self.assertTrue(False, 'Generated invalid XML: "%s"' % e)

    def tearDown(self):
        super(TestSpecialCharacterInURL, self).tearDown()
        try:
            os.remove(self.FILENAME)
        except:
            pass
        finally:
            self.kb.cleanup()


class XMLNodeGeneratorTest(unittest.TestCase):
    def assertValidXML(self, xml):
        etree.fromstring(xml)
        assert 'escape_attr_val' not in xml


class TestHTTPTransaction(XMLNodeGeneratorTest):
    def setUp(self):
        kb.kb.cleanup()
        create_temp_dir()
        HistoryItem().init()

    def tearDown(self):
        remove_temp_dir()
        HistoryItem().clear()
        kb.kb.cleanup()

    def test_render_simple(self):
        url = URL('http://w3af.com/a/b/c.php')
        hdr = Headers([('User-Agent', 'w3af')])
        request = HTTPRequest(url, data='a=1')
        request.set_headers(hdr)

        hdr = Headers([('Content-Type', 'text/html')])
        res = HTTPResponse(200, '<html>', hdr, url, url)

        _id = 1

        h1 = HistoryItem()
        h1.request = request
        res.set_id(_id)
        h1.response = res
        h1.save()

        http_transaction = HTTPTransaction(_id)
        xml = http_transaction.to_string()

        expected = (u'<http-transaction id="1">\n\n'
                    u'    <http-request>\n'
                    u'        <status>POST http://w3af.com/a/b/c.php HTTP/1.1</status>\n'
                    u'        <headers>\n'
                    u'            <header field="User-agent" content="w3af" />\n'
                    u'        </headers>\n'
                    u'        <body content-encoding="base64">YT0x\n</body>\n'
                    u'    </http-request>\n\n'
                    u'    <http-response>\n'
                    u'        <status>HTTP/1.1 200 OK</status>\n'
                    u'        <headers>\n'
                    u'            <header field="Content-Type" content="text/html" />\n'
                    u'        </headers>\n'
                    u'        <body content-encoding="base64">PGh0bWw+\n</body>\n'
                    u'    </http-response>\n\n</http-transaction>')

        self.assertEqual(expected, xml)
        self.assertValidXML(xml)

    def test_cache(self):
        url = URL('http://w3af.com/a/b/c.php')
        hdr = Headers([('User-Agent', 'w3af')])
        request = HTTPRequest(url, data='a=1')
        request.set_headers(hdr)

        hdr = Headers([('Content-Type', 'text/html')])
        res = HTTPResponse(200, '<html>', hdr, url, url)

        _id = 2

        h1 = HistoryItem()
        h1.request = request
        res.set_id(_id)
        h1.response = res
        h1.save()

        http_transaction = HTTPTransaction(_id)

        self.assertFalse(http_transaction.is_in_cache())
        self.assertRaises(Exception, http_transaction.get_node_from_cache)

        # Writes to cache
        xml = http_transaction.to_string()

        expected = (u'<http-transaction id="2">\n\n'
                    u'    <http-request>\n'
                    u'        <status>POST http://w3af.com/a/b/c.php HTTP/1.1</status>\n'
                    u'        <headers>\n'
                    u'            <header field="User-agent" content="w3af" />\n'
                    u'        </headers>\n'
                    u'        <body content-encoding="base64">YT0x\n</body>\n'
                    u'    </http-request>\n\n'
                    u'    <http-response>\n'
                    u'        <status>HTTP/1.1 200 OK</status>\n'
                    u'        <headers>\n'
                    u'            <header field="Content-Type" content="text/html" />\n'
                    u'        </headers>\n'
                    u'        <body content-encoding="base64">PGh0bWw+\n</body>\n'
                    u'    </http-response>\n\n</http-transaction>')
        self.assertEqual(expected, xml)

        # Yup, we're cached
        self.assertTrue(http_transaction.is_in_cache())

        # Make sure they are all the same
        cached_xml = http_transaction.get_node_from_cache()
        self.assertEqual(cached_xml, expected)

        xml = http_transaction.to_string()
        self.assertEqual(expected, xml)


class TestScanInfo(XMLNodeGeneratorTest):
    def test_render_simple(self):
        w3af_core = w3afCore()

        w3af_core.plugins.set_plugins(['sqli'], 'audit')
        w3af_core.plugins.set_plugins(['web_spider'], 'crawl')

        plugin_inst = w3af_core.plugins.get_plugin_inst('crawl', 'web_spider')
        web_spider_options = plugin_inst.get_options()

        w3af_core.plugins.set_plugin_options('crawl', 'web_spider', web_spider_options)

        plugins_dict = w3af_core.plugins.get_all_enabled_plugins()
        options_dict = w3af_core.plugins.get_all_plugin_options()
        scan_target = 'https://w3af.org'

        scan_info = ScanInfo(scan_target, plugins_dict, options_dict)
        xml = scan_info.to_string()

        expected = (u'<scan-info target="https://w3af.org">\n'
                    u'    <audit>\n'
                    u'            <plugin name="sqli">\n'
                    u'            </plugin>\n'
                    u'    </audit>\n'
                    u'    <infrastructure>\n'
                    u'    </infrastructure>\n'
                    u'    <bruteforce>\n'
                    u'    </bruteforce>\n'
                    u'    <grep>\n'
                    u'    </grep>\n'
                    u'    <evasion>\n'
                    u'    </evasion>\n'
                    u'    <output>\n'
                    u'    </output>\n'
                    u'    <mangle>\n'
                    u'    </mangle>\n'
                    u'    <crawl>\n'
                    u'            <plugin name="web_spider">\n'
                    u'                        <config parameter="only_forward" value="False"/>\n'
                    u'                        <config parameter="follow_regex" value=".*"/>\n'
                    u'                        <config parameter="ignore_regex" value=""/>\n'
                    u'            </plugin>\n'
                    u'    </crawl>\n'
                    u'    <auth>\n'
                    u'    </auth>\n'
                    u'</scan-info>')

        self.assertEqual(xml, expected)
        self.assertValidXML(xml)


class TestFinding(XMLNodeGeneratorTest):
    def setUp(self):
        kb.kb.cleanup()
        create_temp_dir()
        HistoryItem().init()

    def tearDown(self):
        remove_temp_dir()
        HistoryItem().clear()
        kb.kb.cleanup()

    def test_render_simple(self):
        _id = 2

        vuln = MockVuln(_id=_id)

        url = URL('http://w3af.com/a/b/c.php')
        hdr = Headers([('User-Agent', 'w3af')])
        request = HTTPRequest(url, data='a=1')
        request.set_headers(hdr)

        hdr = Headers([('Content-Type', 'text/html')])
        res = HTTPResponse(200, '<html>', hdr, url, url)

        h1 = HistoryItem()
        h1.request = request
        res.set_id(_id)
        h1.response = res
        h1.save()

        finding = Finding(vuln)
        xml = finding.to_string()

        expected = (u'<vulnerability id="[2]" method="GET" name="TestCase" plugin="plugin_name" severity="High" url="None" var="None">\n'
                    u'    <description>Foo bar spam eggsFoo bar spam eggsFoo bar spam eggsFoo bar spam eggsFoo bar spam eggsFoo bar spam eggsFoo bar spam eggsFoo bar spam eggsFoo bar spam eggsFoo bar spam eggs</description>\n\n\n'
                    u'    <http-transactions>\n'
                    u'            <http-transaction id="2">\n\n'
                    u'    <http-request>\n'
                    u'        <status>POST http://w3af.com/a/b/c.php HTTP/1.1</status>\n'
                    u'        <headers>\n'
                    u'            <header field="User-agent" content="w3af" />\n'
                    u'        </headers>\n'
                    u'        <body content-encoding="base64">YT0x\n</body>\n'
                    u'    </http-request>\n\n'
                    u'    <http-response>\n'
                    u'        <status>HTTP/1.1 200 OK</status>\n'
                    u'        <headers>\n'
                    u'            <header field="Content-Type" content="text/html" />\n'
                    u'        </headers>\n'
                    u'        <body content-encoding="base64">PGh0bWw+\n</body>\n'
                    u'    </http-response>\n\n'
                    u'</http-transaction>\n'
                    u'    </http-transactions>\n'
                    u'</vulnerability>')

        self.assertEqual(xml, expected)
        self.assertValidXML(xml)
