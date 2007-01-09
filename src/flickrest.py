# flickrpc -- a Flickr client library.
#
# Copyright (C) 2007 Ross Burton <ross@burtonini.com>
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51 Franklin
# St, Fifth Floor, Boston, MA 02110-1301 USA

import logging, md5, os, mimetools, urllib
from twisted.internet import defer
from twisted.python.failure import Failure
from twisted.web import client
from elementtree import ElementTree

class FlickrError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self)
        self.code = int(code)
        self.message = message
    
    def __str__(self):
        return "%d: %s" % (self.code, self.message)

(SIZE_SQUARE,
 SIZE_THUMB,
 SIZE_SMALL,
 SIZE_MEDIUM,
 SIZE_LARGE) = range (0, 5)

class Flickr:
    endpoint = "http://api.flickr.com/services/rest/?"
    
    def __init__(self, api_key, secret, perms="read"):
        self.__methods = {}
        self.api_key = api_key
        self.secret = secret
        self.perms = perms
        self.token = None
        self.logger = logging.getLogger('flickrest')
    
    def __repr__(self):
        return "<FlickREST>"
    
    def __getTokenFile(self):
        """Get the filename that contains the authentication token for the API key"""
        return os.path.expanduser(os.path.join("~", ".flickr", self.api_key, "auth.xml"))
    
    def __sign(self, kwargs):
        kwargs['api_key'] = self.api_key
        # If authenticating we don't yet have a token
        if self.token:
            kwargs['auth_token'] = self.token
        s = []
        for key in kwargs.keys():
            s.append("%s%s" % (key, kwargs[key]))
        s.sort()
        sig = md5.new(self.secret + ''.join(s)).hexdigest()
        kwargs['api_sig'] = sig

    def __call(self, method, kwargs):
        kwargs["method"] = method
        self.__sign(kwargs)
        self.logger.info("Calling %s" % method)
        return client.getPage(Flickr.endpoint, method="POST",
                              headers={"Content-Type": "application/x-www-form-urlencoded"},
                              postdata=urllib.urlencode(kwargs))
        
    def __getattr__(self, method, **kwargs):
        method = "flickr." + method.replace("_", ".")
        if not self.__methods.has_key(method):
            def proxy(method=method, **kwargs):
                d = defer.Deferred()
                def cb(data):
                    self.logger.info("%s returned" % method)
                    xml = ElementTree.XML(data)
                    if xml.tag == "rsp" and xml.get("stat") == "ok":
                        d.callback(xml)
                    elif xml.tag == "rsp" and xml.get("stat") == "fail":
                        err = xml.find("err")
                        d.errback(Failure(FlickrError(err.get("code"), err.get("msg"))))
                    else:
                        # Fake an error in this case
                        d.errback(Failure(FlickrError(0, "Invalid response")))
                self.__call(method, kwargs).addCallbacks(cb, lambda fault: d.errback(fault))
                return d
            self.__methods[method] = proxy
        return self.__methods[method]

    @staticmethod
    def __encodeForm(inputs):
        """
        Takes a dict of inputs and returns a multipart/form-data string
        containing the utf-8 encoded data. Keys must be strings, values
        can be either strings or file-like objects.
        """
        boundary = mimetools.choose_boundary()
        lines = []
        for key, val in inputs.items():
            lines.append("--" + boundary.encode("utf-8"))
            header = 'Content-Disposition: form-data; name="%s";' % key
            # Objects with name value are probably files
            if hasattr(val, 'name'):
                header += 'filename="%s";' % os.path.split(val.name)[1]
                lines.append(header)
                header = "Content-Type: application/octet-stream"
            lines.append(header)
            lines.append("")
            # If there is a read attribute, it is a file-like object, so read all the data
            if hasattr(val, 'read'):
                lines.append(val.read())
            # Otherwise just hope it is string-like and encode it to
            # UTF-8. TODO: this breaks when val is binary data.
            else:
                lines.append(val.encode('utf-8'))
        # Add final boundary.
        lines.append("--" + boundary.encode("utf-8"))
        return (boundary, '\r\n'.join(lines))
    
    # TODO: add is_public, is_family, is_friends arguments
    def upload(self, filename=None, imageData=None, title=None, desc=None, tags=None):
        # Sanity check the arguments
        if filename is None and imageData is None:
            raise ValueError("Need to pass either filename or imageData")
        if filename and imageData:
            raise ValueError("Cannot pass both filename and imageData")

        kwargs = {}
        if title:
            kwargs['title'] = title
        if desc:
            kwargs['description'] = desc
        if tags:
            kwargs['tags'] = tags
        self.__sign(kwargs)
        
        if imageData:
            kwargs['photo'] = imageData
        else:
            kwargs['photo'] = file(filename, "rb")

        (boundary, form) = self.__encodeForm(kwargs)
        headers= {
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
            "Content-Length": str(len(form))
            }
        return client.getPage("http://api.flickr.com/services/upload/", method="POST",
                              headers=headers, postdata=form)

    def authenticate_2(self, state):
        d = defer.Deferred()
        def gotToken(e):
            # Set the token
            self.token = e.find("auth/token").text
            # Cache the authentication
            filename = self.__getTokenFile()
            path = os.path.dirname(filename)
            if not os.path.exists(path):
                os.makedirs(path, 0700)
            f = file(filename, "w")
            f.write(ElementTree.tostring(e))
            f.close()
            # Callback to the user
            d.callback(True)
        self.auth_getToken(frob=state['frob']).addCallbacks(gotToken, lambda fault: d.errback(fault))
        return d

    def authenticate_1(self):
        """Attempts to log in to Flickr. The return value is a Twisted Deferred
        object that callbacks when the first part of the authentication is
        completed.  If the result passed to the deferred callback is None, then
        the required authentication was locally cached and you are
        authenticated.  Otherwise the result is a dictionary, you should open
        the URL specified by the 'url' key and instruct the user to follow the
        instructions.  Once that is done, pass the state to
        flickrest.authenticate_2()."""

        filename = self.__getTokenFile()
        if os.path.exists(filename):
            try:
                e = ElementTree.parse(filename).getroot()
                self.token = e.find("auth/token").text
                return defer.succeed(None)
            except:
                # TODO: print the exception to stderr?
                pass
        
        d = defer.Deferred()
        def gotFrob(xml):
            frob = xml.find("frob").text
            keys = { 'perms': self.perms,
                     'frob': frob }
            self.__sign(keys)
            url = "http://flickr.com/services/auth/?api_key=%(api_key)s&perms=%(perms)s&frob=%(frob)s&api_sig=%(api_sig)s" % keys
            d.callback({'url': url, 'frob': frob})
        self.auth_getFrob().addCallbacks(gotFrob, lambda fault: d.errback(fault))
        return d

    @staticmethod
    def get_photo_url(photo, size=SIZE_MEDIUM):
        if photo is None:
            return None

        # Handle medium as the default
        suffix = ""
        if size == SIZE_SQUARE:
            suffix = "_s"
        elif size == SIZE_THUMB:
            suffix = "_t"
        elif size == SIZE_SMALL:
            suffix = "_m"
        elif size == SIZE_LARGE:
            suffix = "_b"

        return "http://static.flickr.com/%s/%s_%s%s.jpg" % (photo.get("server"), photo.get("id"), photo.get("secret"), suffix)