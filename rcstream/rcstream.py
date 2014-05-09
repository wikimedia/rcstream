# -*- coding: utf-8 -*-
"""
  RCStream: Broadcast MediaWiki recent changes over WebSockets

  Usage: rcstream [-h] [--debug] [--logfile LOGFILE] server redis

  positional arguments:
    SERVER_ADDRESS     Server address (host:port)
    REDIS_URL          URL of Redis instance

  optional arguments:
    -h, --help         show this help message and exit
    --debug            Log verbosely
    --logfile LOGFILE  Log to LOGFILE

  See <https://wikitech.wikimedia.org/wiki/rcstream> for more.

"""
from gevent import monkey
monkey.patch_all()

import argparse
import json
import logging
import sys

import gevent
import redis
import socketio
import socketio.namespace
import socketio.server


log = logging.getLogger(__name__)


class WsgiBackendLogAdapter(logging.LoggerAdapter):
    """Log adapter that annotates log records with client IP."""

    def process(self, msg, kwargs):
        # Security alert! We're assuming we're behind a proxy which sets
        # X-Forwarded-For. Otherwise this header could be spoofed!
        xff = self.extra.get('HTTP_X_FORWARDED_FOR', '').split(',')
        client_ip = xff[0] if xff else self.extra['REMOTE_ADDRESS']
        return '[%s] %s' % (client_ip, msg), kwargs


class WikiNamespace(socketio.namespace.BaseNamespace):
    """A socket.io namespace that allows clients to subscribe to the
    recent changes stream of individual wikis."""

    MAX_SUBSCRIPTIONS = 100

    def initialize(self):
        self.session['wikis'] = set()
        self.logger = WsgiBackendLogAdapter(log, self.environ)

    def process_packet(self, packet):
        self.logger.info(json.dumps(packet, sort_keys=True))
        super(WikiNamespace, self).process_packet(packet)

    def on_subscribe(self, wikis):
        if not isinstance(wikis, list):
            wikis = [wikis]
        subscriptions = self.session['wikis']
        for wiki in wikis:
            if not isinstance(wiki, basestring):
                continue
            if wiki in subscriptions:
                continue
            if len(subscriptions) >= self.MAX_SUBSCRIPTIONS:
                return self.error('subscribe_error', 'Too many subscriptions')
            subscriptions.add(wiki)

    def on_unsubscribe(self, wikis):
        if not isinstance(wikis, list):
            wikis = [wikis]
        subscriptions = self.session['wikis']
        for wiki in wikis:
            if not isinstance(wiki, basestring):
                continue
            subscriptions.discard(wiki)


class ChangesPubSub(socketio.server.SocketIOServer):
    """A socket.io WSGI server for recent changes."""

    namespaces = {'/rc': WikiNamespace}

    def __init__(self, server_address, redis_connection):
        self.queue = gevent.queue.Channel()
        self.redis_connection = redis_connection
        self.server_address = server_address
        super(ChangesPubSub, self).__init__(server_address, self.on_request)

    def serve_forever(self):
        for func in (self.publish, self.subscribe):
            greenlet = gevent.Greenlet(func)
            greenlet.link_exception(self.on_error)
            greenlet.start()
        super(ChangesPubSub, self).serve_forever()

    def on_request(self, environ, start_response):
        """A WSGI application function."""
        if 'socketio' in environ:
            socketio.socketio_manage(environ, self.namespaces)
        start_response('404 Not Found', [])
        return ['404 Not Found']

    def on_error(self, greenlet):
        log.exception(greenlet.exception)
        sys.exit(1)

    def publish(self):
        base_event = dict(type='event', name='change', endpoint='/rc')
        for change in self.queue:
            wiki = change['server_name']
            event = dict(base_event, args=(change,))
            for client in self.sockets.values():
                subscriptions = client.session.get('wikis', ())
                if '*' in subscriptions or wiki in subscriptions:
                    client.send_packet(event)

    def subscribe(self):
        pubsub = self.redis_connection.pubsub()
        pubsub.psubscribe('rc.*')
        for message in pubsub.listen():
            if message['type'] == 'pmessage':
                data = json.loads(message['data'])
                self.queue.put(data)


def parse_address(addr):
    host, port = addr.split(':')
    return host, int(port)


arg_parser = argparse.ArgumentParser(
    description='Broadcast MediaWiki recent changes over WebSockets',
    epilog='See <https://wikitech.wikimedia.org/wiki/rcstream> for more.',
    fromfile_prefix_chars='@',
)
arg_parser.add_argument('server', help='Server address (host:port)',
                        type=parse_address)
arg_parser.add_argument('redis', help='URL of Redis instance',
                        type=redis.StrictRedis.from_url)
arg_parser.add_argument('--verbose', action='store_const', dest='loglevel',
                        const=logging.DEBUG, default=logging.INFO)
arg_parser.add_argument('--logfile', help='Log to this file')
args = arg_parser.parse_args()

logging.basicConfig(filename=args.logfile, level=args.loglevel,
                    format='[%(asctime)s] %(message)s')
log.info('Listening on %s:%s' % args.server)
ChangesPubSub(args.server, args.redis).serve_forever()