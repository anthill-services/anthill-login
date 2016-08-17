
from tornado.gen import coroutine, Return

from model import authenticator
from model.key import KeyNotFound
from abc import abstractmethod, ABCMeta


class SocialAuthenticator(authenticator.Authenticator):
    """
    Abstract authenticator to social networks (google, facebook etc)

    """

    __metaclass__ = ABCMeta

    def __init__(self, application, credential_type):
        super(SocialAuthenticator, self).__init__(application, credential_type)

    @abstractmethod
    def new_private_key(self, data):
        raise NotImplementedError()

    @coroutine
    def get_private_key(self, gamespace, data=None):

        if not data:
            try:
                data = yield self.get_key(gamespace, self.type())
            except KeyNotFound:
                raise authenticator.AuthenticationError("key_not_found")

        raise Return(self.new_private_key(data))

    @coroutine
    def get_app_id(self, gamespace, data=None):
        private_key = yield self.get_private_key(gamespace, data=data)
        raise Return(private_key.get_app_id())