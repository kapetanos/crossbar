#####################################################################################
#
#  Copyright (c) Crossbar.io Technologies GmbH
#  SPDX-License-Identifier: EUPL-1.2
#
#####################################################################################

import json

from autobahn import util
from autobahn.wamp import auth
from autobahn.wamp import types
from autobahn.util import hltype

from crossbar.router.auth.pending import PendingAuth

import txaio
from txaio import make_logger

__all__ = ('PendingAuthWampCra', )


class PendingAuthWampCra(PendingAuth):
    """
    Pending WAMP-CRA authentication.
    """

    AUTHMETHOD = 'wampcra'

    log = make_logger()

    def __init__(self, pending_session_id, transport_info, realm_container, config):
        super(PendingAuthWampCra, self).__init__(
            pending_session_id,
            transport_info,
            realm_container,
            config,
        )

        # The signature we expect the client to send in AUTHENTICATE.
        self._signature = None

    def _compute_challenge(self, user):
        """
        Returns: challenge, signature
        """
        challenge_obj = {
            'authid': self._authid,
            'authrole': self._authrole,
            'authmethod': self._authmethod,
            'authprovider': self._authprovider,
            'session': self._session_details['session'],
            'nonce': util.newid(64),
            'timestamp': util.utcnow()
        }
        challenge = json.dumps(challenge_obj, ensure_ascii=False)

        # Sometimes, if it doesn't have to be Unicode, PyPy won't make it
        # Unicode. Make it Unicode, even if it's just ASCII.
        if not isinstance(challenge, str):
            challenge = challenge.decode('utf8')

        secret = user['secret'].encode('utf8')
        signature = auth.compute_wcs(secret, challenge.encode('utf8')).decode('ascii')

        # extra data to send to client in CHALLENGE
        extra = {'challenge': challenge}

        # when using salted passwords, provide the client with
        # the salt and then PBKDF2 parameters used
        if 'salt' in user:
            extra['salt'] = user['salt']
            extra['iterations'] = user.get('iterations', 1000)
            extra['keylen'] = user.get('keylen', 32)

        return extra, signature

    def hello(self, realm, details):

        # remember the realm the client requested to join (if any)
        self._realm = realm

        # remember the authid the client wants to identify as (if any)
        self._authid = details.authid

        def on_authenticate_ok(principal):
            error = self._assign_principal(principal)
            if error:
                return error

            # now compute CHALLENGE.Extra and signature expected
            extra, self._signature = self._compute_challenge(principal)
            return types.Challenge(self._authmethod, extra)

        def on_authenticate_error(err):
            return self._marshal_dynamic_authenticator_error(err)

        # use static principal database from configuration
        if self._config['type'] == 'static':

            self._authprovider = 'static'

            if self._authid in self._config.get('users', {}):

                principal = self._config['users'][self._authid]

                error = self._assign_principal(principal)
                if error:
                    return error

                # now compute CHALLENGE.Extra and signature as
                # expected for WAMP-CRA
                extra, self._signature = self._compute_challenge(principal)

                return types.Challenge(self._authmethod, extra)
            else:
                return types.Deny(message='no principal with authid "{}" exists'.format(details.authid))

        # use configured procedure to dynamically get a ticket for the principal
        elif self._config['type'] == 'dynamic':

            self._authprovider = 'dynamic'

            init_d = txaio.as_future(self._init_dynamic_authenticator)

            def init(result):
                if result:
                    return result

                self._session_details['authmethod'] = self._authmethod  # from AUTHMETHOD, via base
                self._session_details['authid'] = details.authid
                self._session_details['authrole'] = details.authrole
                self._session_details['authextra'] = details.authextra

                d = self._authenticator_session.call(self._authenticator, realm, details.authid, self._session_details)
                d.addCallbacks(on_authenticate_ok, on_authenticate_error)

                return d

            init_d.addBoth(init)
            return init_d

        elif self._config['type'] == 'function':

            self._authprovider = 'function'

            init_d = txaio.as_future(self._init_function_authenticator)

            def init(result):
                if result:
                    return result

                self._session_details['authmethod'] = self._authmethod  # from AUTHMETHOD, via base
                self._session_details['authid'] = details.authid
                self._session_details['authrole'] = details.authrole
                self._session_details['authextra'] = details.authextra

                auth_d = txaio.as_future(self._authenticator, realm, details.authid, self._session_details)
                auth_d.addCallbacks(on_authenticate_ok, on_authenticate_error)

                return auth_d

            init_d.addBoth(init)
            return init_d

        else:
            # should not arrive here, as config errors should be caught earlier
            return types.Deny(message='invalid authentication configuration (authentication type "{}" is unknown)'.
                              format(self._config['type']))

    def authenticate(self, signature):

        if signature == self._signature:
            # signature was valid: accept the client
            return self._accept()
        else:
            # signature was invalid: deny the client
            self.log.warn('{func}: WAMP-CRA client signature is invalid (expected {expected} but got {signature})',
                          func=hltype(self.authenticate),
                          expected=self._signature,
                          signature=signature)
            return types.Deny(message='WAMP-CRA client signature is invalid')
