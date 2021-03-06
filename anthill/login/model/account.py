
from anthill.common import access, sign, discover, update

from anthill.common.internal import Internal, InternalError
from anthill.common.access import AccessToken
from anthill.common.database import DatabaseError
from anthill.common.model import Model
from anthill.common.gen import AccessTokenGenerator

from . import authenticator

from . credential import CredentialNotFound
from . gamespace import GamespaceNotFound
from . access import NoScopesFound

import ujson
import logging


class AccountModel(Model):
    """

    This model represents user accounts. The idea is as follows:

    User may has multiple unique credentials (google, facebook, anonymous) but yet it has one account:

        google:123456 -> account:1
        facebook:123456 -> account:1
        anonymous:xxx-xxx-xxx-xxx -> account:1

    Whenever user authorizes, it can attach and detach credentials.

    Yet user can't have two credentials of same type attached to the same account. That means this:

        google:123456 -> account:1
        google:678910 -> account:1

    If a conflict. So is this:

        google:123456 -> account:1
        google:123456 -> account:2

    Such kind of issues are resolved by user during authorization.

    """

    # special kind of credentials treated as 'anonymous' - the ones is easy create automatically
    LOCAL_CREDENTIALS = ["anonymous", "dev"]

    def get_setup_tables(self):
        return ["accounts"]

    async def setup_table_accounts(self):
        # that should create an account with @1
        await self.create_account()

    def get_setup_db(self):
        return self.db

    def has_delete_account_event(self):
        return True

    async def accounts_deleted(self, gamespace, accounts, gamespace_only):
        if gamespace_only:
            return

        try:
            async with self.db.acquire() as db:
                await db.execute(
                    """
                        DELETE
                        FROM `account_credentials`
                        WHERE `account_id` IN %s
                    """, accounts)
                await db.execute(
                    """
                        DELETE FROM `accounts`
                        WHERE `account_id` IN %s;
                    """, accounts)
        except DatabaseError as e:
            raise AccountError("Failed to delete accounts: " + e.args[1])

    async def __import_social_connections__(self, gamespace, credential, username, auth_response):
        """
        Imports social connections (friends) to the social service.

        When user authorises on credential that have 'social profile' (social_profile)
        it gets imported to the social service for later use.

        Social connections are imported not in account level, but in credential level. That means social service
        doesn't care much about accounts, but care about credentials.

        """

        try:
            await self.internal.request(
                "social",
                "import_social",
                gamespace=gamespace,
                credential=credential,
                username=username,
                auth=auth_response.data)

        except InternalError as e:
            raise AuthenticationError(
                e.code,
                "failed_to_import_social",
                info=e.body)

        except discover.DiscoveryError as e:
            logging.warning("Failed to discover social: " + str(e.code) + " " + e.message)
            return False

    def __init__(self, application, db):
        self.db = db
        self.application = application
        self.internal = Internal()

    async def __merge_accounts__(self, attach_to, credential, resolve, gamespace, db=None):

        """
        Merges two credentials together. User should choose between two options:

                 my account                    the one attaching to
                                   ====>
                @account_mine                     @account_attach
             (<credential_mine>)               (<credential_attach>)

        :param attach_to: A valid access token with account and credential inside.
        :param credential: A credential going to be merged
        :param resolve: In case of conflict, user may pass an option how to resolve it:

            local - Choose to use account in <attach_to>, so credential <credential> will be moved
                from <account_mine> to <account_attach>
            remote - Choose to use account in <account_mine>, so credential <credential_attach> will be moved
                from <account_attach> to <account_mine>
            not_mine - the account of my credential <credential> is not mine,
                so ignore it, and stick to account in <attach_to>

        :param gamespace: A gamespace a merge happens in. Please note that gamespace have rarely something to do
            with gamespaces.

        :param db: (optional) a db for batched requests

        Such errors are possible:

        1. "merge_required"
            When credentials <attach_to> and <credential> both look to a different accounts.
            In that case a resolve_token is returned: it's a short-lived token for user to choose account and proceed.

        2. "multiple_accounts_attached"
            When same credential looks BOTH to account1 and account2. If that case user should choose account the user
            want to stick with, the other one will be detached.

        :return: A chosen account after merge
        """

        cred_split = access.parse_account(credential)
        credential_type = cred_split[0]

        account_attach = attach_to.account
        credential_attach = attach_to.name

        credential_mine = credential

        credentials_data = self.application.credentials
        accounts_data = self.application.accounts

        same_credential = None

        attached_credentials = await credentials_data.list_account_credentials(
            account_attach,
            [credential_type],
            db=db)

        if len(attached_credentials) > 0:
            same_credential = attached_credentials[0]

        accounts = await credentials_data.list_accounts(
            credential_mine, db=db)

        tokens = self.application.tokens

        if same_credential:
            if same_credential == credential_mine:
                return account_attach

            if not accounts:
                account_mine = await self.create_account(db=db)

                if account_mine is None:
                    raise AuthenticationError(
                        500,
                        "internal_error",
                        info="Failed to create new account.")

                await credentials_data.attach(credential_mine, account_mine, db=db)
            elif len(accounts) == 1:
                account_mine = accounts[0]
            else:
                raise AuthenticationError(
                    409,
                    "multiple_accounts_attached",
                    info="Credential '{0}' has multiple accounts attached.".format(credential_mine))

            await credentials_data.detach(
                credential_attach,
                account_attach,
                db=db)

            await credentials_data.attach(
                credential_attach,
                account_mine, db=db)

            await tokens.invalidate_account(account_attach)

            return account_mine

        else:
            if not accounts:
                await credentials_data.attach(
                    credential_mine,
                    account_attach, db=db)

                return account_attach
            elif len(accounts) == 1:
                account_mine = accounts[0]

                if resolve is None:
                    resolve_token = AccessTokenGenerator.generate(
                        sign.TOKEN_SIGNATURE_RSA,
                        ["resolve_conflict"],
                        {
                            AccessToken.GAMESPACE: gamespace
                        },
                        credential,
                        token_only=True)

                    accounts = {
                        "local": {
                            "account": account_attach,
                            "credential": credential_attach
                        },
                        "remote": {
                            "account": account_mine,
                            "credential": credential_mine
                        }
                    }

                    try:
                        profiles = await self.internal.request(
                            "profile",
                            "mass_profiles",
                            action="get_public",
                            accounts=[account_attach, account_mine],
                            gamespace=gamespace)

                    except InternalError as e:
                        logging.exception("failed to get profiles for conflict: %s, %s", e.body, e.code)
                    else:
                        accounts["local"]["profile"] = profiles[account_attach]
                        accounts["remote"]["profile"] = profiles[account_mine]

                    raise AuthenticationError(
                        409,
                        "merge_required",
                        accounts=accounts,
                        resolve_token=resolve_token
                    )
                else:

                    async def not_mine():
                        await credentials_data.detach(
                            credential_mine,
                            account_mine,
                            accounts_data=accounts_data,
                            db=db)

                        await credentials_data.attach(
                            credential_mine,
                            account_attach,
                            db=db)

                        return account_attach

                    async def local():

                        await credentials_data.detach(
                            credential_mine,
                            account_mine,
                            accounts_data=accounts_data,
                            db=db)

                        await credentials_data.attach(
                            credential_mine,
                            account_attach,
                            db=db)

                        anonymous_credentials = await credentials_data.list_account_credentials(
                            account_mine,
                            AccountModel.LOCAL_CREDENTIALS,
                            db=db)

                        for anon_credential in anonymous_credentials:
                            await credentials_data.attach(
                                anon_credential,
                                account_attach,
                                db=db)

                        await tokens.invalidate_account(account_mine)

                        return account_attach

                    async def remote():
                        await credentials_data.detach(
                            credential_attach,
                            account_attach,
                            db=db)

                        await tokens.invalidate_account(account_attach)

                        await credentials_data.attach(
                            credential_attach,
                            account_mine,
                            db=db)

                        return account_mine

                    merge_options = {
                        "not_mine": not_mine,
                        "local": local,
                        "remote": remote
                    }

                    try:
                        result = await merge_options[resolve]()
                        return result

                    except KeyError:
                        raise AuthenticationError(
                            400,
                            "unknown_merge_option",
                            info="Unknown merge option: '{0}'.".format(resolve))

            else:
                raise AuthenticationError(
                    409,
                    "multiple_accounts_attached",
                    info="Credential '{0}' has multiple accounts attached.".format(credential_mine))

    async def __multiple_accounts_attached__(self, gamespace_id, credential, accounts):

        """
        User is in a state when same credential attached to a two (or more) different accounts:

            credential1 -> account1
            credential1 -> account2

        :param gamespace_id: A gamespace
        :param credential: A credential attached to multiple accounts
        :param accounts: A list of accounts being attached to that credential
        :return:
        """

        # generate special resolve_token for a user

        resolve_token = AccessTokenGenerator.generate(
            sign.TOKEN_SIGNATURE_RSA,
            ["resolve_conflict"],
            {
                AccessToken.GAMESPACE: gamespace_id
            },
            credential,
            token_only=True)

        try:
            # collect some information about these accounts

            account_profiles = await self.internal.request(
                "profile", "mass_profiles",
                accounts=accounts,
                gamespace=gamespace_id,
                action="get_public")

        except InternalError as e:
            account_profiles = {}
            logging.exception("failed to get profiles for multiple_accounts_attached: %s, %s", e.body, e.code)

        accounts_summary = [
            {
                "account": account,
                "profile": (account_profiles[account] if account in account_profiles else {})
            } for account in accounts
        ]

        raise AuthenticationError(
            300,
            "multiple_accounts_attached",
            resolve_token=resolve_token,
            accounts=accounts_summary,
            info="Conflict: More than one credentials attached to this account.")

    async def attach_account(self, args, env=None):

        """
        Attaches a credential from token <access_token> to an account from token <attach_to>
        """
        try:
            access_token = args["access_token"]
            attach_to = args["attach_to"]
            requested_scopes = access.parse_scopes(args["scopes"])
        except KeyError:
            raise AuthenticationError(
                400,
                "missing_argument",
                info="Some argument is missing.")

        tokens = self.application.tokens

        access_token = AccessToken(access_token)
        attach_to = AccessToken(attach_to)

        if not (await tokens.validate(access_token)):
            raise AuthenticationError(
                403,
                "access_token_invalid",
                info="Access token is not valid")

        if not (await tokens.validate(attach_to)):
            raise AuthenticationError(
                403,
                "attach_to_token_invalid",
                info="Token attach to is not valid")

        token_gamespace = access_token.get(
            AccessToken.GAMESPACE)

        attach_to_gamespace = attach_to.get(
            AccessToken.GAMESPACE)

        if token_gamespace != attach_to_gamespace:
            raise AuthenticationError(
                400,
                "wrong_gamespace",
                info="These tokens are from different gamespaces")

        gamespace_id = attach_to_gamespace

        async with self.db.acquire() as db:

            # take the credential from attach_to
            credential = access_token.name

            account = await self.__merge_accounts__(
                attach_to,
                credential,
                None,
                gamespace_id,
                db=db)

            logging.debug("Merged into {0}".format(account))

            # if there's no conflict, complete with last step
            result = await self.proceed_authentication(
                account,
                credential,
                gamespace_id,
                requested_scopes,
                args,
                env=env,
                db=db)

            return result

    async def authorize(self, args, env=None):
        """
        The method that authenticates a user.
        :param args:
            'credential' - a user's credential
            'scopes' - a list of access scopes the user would like to get
            'gamespace' - a gamespace user would like to work with
            'attach_to' (optional) - an access token attach to in case of merge happening
            'import_profile' (optional) - in case of 'social' type of credential, do import basic social info into a
                user profile (default is true)

            Some other arguments may apply depending on the credential type. For example, 'code' for google.
        :param env:
            Environment variables passed back to authentication process (like user's ip address)
        """

        try:
            cred_type = args["credential"]
            requested_scopes = access.parse_scopes(args["scopes"])

            gamespace_id = args.get("gamespace_id")

            if not gamespace_id:
                gamespace_name = args["gamespace"]
            else:
                gamespace_name = ""

        except KeyError:
            raise AuthenticationError(
                400,
                "missing_argument",
                info="Some argument is missing.")

        attach_to = args.get("attach_to", None)

        credentials_data = self.application.credentials
        cred_types = credentials_data.credential_types

        if cred_type not in cred_types:
            raise AuthenticationError(
                400,
                "unknown_credential",
                info="Unknown credential type: " + cred_type)

        tokens = self.application.tokens

        credential_authorizer = cred_types[cred_type]

        if attach_to is not None:
            token = AccessToken(attach_to)

            if not (await tokens.validate(token)):
                raise AuthenticationError(
                    403,
                    "attach_to_token_invalid",
                    info="Token attach to is not valid")

            attach_to = token

        async with self.db.acquire() as db:

            if not gamespace_id:
                try:
                    gamespace_id = await self.application.gamespaces.find_gamespace(
                        gamespace_name,
                        db=db)

                except GamespaceNotFound:
                    raise AuthenticationError(
                        404,
                        "no_such_gamespace",
                        info="Gamespace '{0}' was not found.".format(gamespace_name))

            try:
                result = await credential_authorizer.authorize(
                    gamespace_id,
                    args,
                    db=db,
                    env=env)

            except authenticator.AuthenticationError as e:

                raise AuthenticationError(
                    403,
                    e.message,
                    info="Failed to authorize with such username/password",
                    error=e.code)

            if result.response is not None and result.response.import_social:
                await self.__import_social_connections__(
                    gamespace_id,
                    result.credential,
                    result.username,
                    result.response)

            credential = result.credential + ":" + result.username

            if attach_to:
                account = await self.__merge_accounts__(
                    attach_to,
                    credential,
                    None,
                    gamespace_id,
                    db=db)

                logging.debug("Merged into {0}".format(account))
            else:
                accounts = await credentials_data.list_accounts(
                    credential,
                    db=db)

                if not accounts:
                    account = await self.create_account(db=db)

                    if account is None:
                        raise AuthenticationError(
                            500,
                            "internal_error",
                            info="Failed to create new account.")

                    logging.info("New account created: '%s'.", account)

                    await credentials_data.attach(
                        credential,
                        account,
                        db=db)

                elif len(accounts) == 1:
                    account = accounts[0]
                else:
                    await self.__multiple_accounts_attached__(
                        gamespace_id,
                        credential,
                        accounts)

                    return

            result = await self.proceed_authentication(
                account,
                credential,
                gamespace_id,
                requested_scopes,
                args,
                env=env,
                db=db)

            return result

    async def proceed_authentication(self, account, credential, gamespace_id, requested_scopes, args, env, db=None):

        """
        The last one, final step in authorization. All conflicts are resolved, all accesses are gathered.

        :param account: User account
        :param credential: User credential
        :param gamespace_id: A gamespace user would like to work with
        :param requested_scopes: A scopes user would like to have
        :param args: other arguments:

            'as' - each user can have only one live access token for a <system>, so when he authorizes in the <system>,
                the old <system> token gets invalidated. Default <system> is 'def', but if user wants to hold the
                token alive, he might authorize in different system. For example, a game and a web site could
                live in different systems 'def' and 'www'.

            'unique' - if value is 'false', it would be possible to have several valid access tokens pointing to the
                same account. In fact, that kind of access token would be impossible to invalidate since there is no
                record for it.  A special access scope 'auth_non_unique' is required to proceed such
                authentication.

            'should_have' - a comma-separated list of scopes user should definitely have, or 403.
                '*' means he fine with everything he gets.

            'import_profile' (optional) - in case of 'social' type of credential, do import basic social info into a
                user profile (default is true)

        :param env: environment variables passed back to authentication process (like user's ip address)
        :param db:

        :return: A signed access token.

        """

        access_data = self.application.access
        gamespaces_data = self.application.gamespaces
        cred_types = self.application.credentials.credential_types

        cred_type, username = access.parse_account(credential)

        auth_as = args.get("as")
        if auth_as:
            if not access.validate_token_name(auth_as):
                raise AuthenticationError(
                    400,
                    "bad_auth_as",
                    info="Bad auth as name format : " + auth_as)

        if cred_type not in cred_types:
            raise AuthenticationError(
                400,
                "unknown_credential",
                info="Unknown credential type: " + cred_type)

        credential_authenticator = cred_types[cred_type]

        fetch_profile = args.get("import_profile", "true") == "true"
        profile_data = None

        # if the credential is 'social', attach the credential from social network to an account

        if credential_authenticator.social_profile():
            try:
                profile_data = await self.internal.request(
                    "social",
                    "attach_account",
                    gamespace=gamespace_id,
                    credential=cred_type,
                    username=username,
                    account=account,
                    env=env,
                    fetch_profile=fetch_profile)

            except InternalError as e:
                logging.warning("Failed to get profile_data: %s %s", e.code, e.body)

        should_have = args.get("should_have", "*")

        if should_have != "*":
            should_have_scopes = access.parse_scopes(should_have)

        def _have_scope(a_scope):
            return should_have == "*" or (a_scope in should_have_scopes)

        # the scopes user has

        try:
            user_scopes = await access_data.get_account_access(gamespace_id, account, db=db)
        except NoScopesFound:
            user_scopes = set()

        # the scopes gamespace has

        try:
            gamespace_scopes_data = await gamespaces_data.get_gamespace_access_scopes(
                gamespace_id, db=db)

        except GamespaceNotFound:
            raise AuthenticationError(
                404,
                "no_such_gamespace",
                info="Gamespace ID '{0}' was not found.".format(gamespace_id))

        gamespace_scopes = access.parse_scopes(gamespace_scopes_data)

        user_scopes.update(gamespace_scopes)

        for scope in requested_scopes:
            if (scope not in user_scopes) and _have_scope(scope):
                raise AuthenticationError(
                    403,
                    "scope_restricted",
                    info="User '{0}' has no scope '{1}' asked.".format(credential, scope),
                    credential=credential)

        if args.get("unique", "true") == "false":
            if "auth_non_unique" not in user_scopes:
                raise AuthenticationError(
                    403,
                    "non_unique_token_restricted",
                    info="User '{0}' has no access to disable unique "
                         "tokens (scope 'auth_non_unique' is required).".format(credential),
                    credential=credential)
            unique = False
        else:
            unique = True

        cross = set(requested_scopes) & set(user_scopes)
        allowed_scopes = list(cross)

        # update account info

        account_info = args.get("info")
        if account_info:

            try:
                account_info = ujson.loads(account_info)
            except (KeyError, ValueError):
                raise AuthenticationError(
                    400,
                    "bad_account_info",
                    info="The field 'info' is corrupted.")

            if not isinstance(account_info, dict):
                raise AuthenticationError(
                    400,
                    "bad_account_info",
                    info="The field 'info' should be a JSON dictionary.")

            await self.update_account_info(account, account_info)

        # FINAL STEP: access token sign

        additional_containers = {
            AccessToken.ACCOUNT: str(account),
            AccessToken.GAMESPACE: str(gamespace_id)
        }

        if unique:
            # no 'issuer' field - nowhere to check
            additional_containers[AccessToken.ISSUER] = "login"

        res = AccessTokenGenerator.generate(
            sign.TOKEN_SIGNATURE_RSA,
            allowed_scopes,
            additional_containers,
            credential)

        token = res["key"]
        uuid = res["uuid"]
        expires = res["expires"]
        scopes = res["scopes"]

        # store the token in key/value storage

        tokens = self.application.tokens

        if unique:
            await tokens.save_token(
                account,
                uuid,
                expires,
                name=auth_as)

        # if credential is 'social', store the stuff from social network (avatar, nickname) to a profile

        if credential_authenticator.social_profile():
            try:
                if profile_data:
                    await self.internal.request(
                        "profile",
                        "update_profile",
                        fields=profile_data,
                        gamespace_id=gamespace_id,
                        account_id=account)

            except InternalError as e:
                logging.warning("Failed to update user profile: %s %s", e.code, e.body)

        logging.info(
            "Authorised user {0} (scopes: {1})".format(
                credential, ",".join(allowed_scopes)))

        # here we go

        return {
            "token": token,
            "account": account,
            "credential": credential,
            "scopes": scopes
        }

    async def delete_account(self, account_id, db=None):

        """
        Deletes an account.
        """

        credentials_data = self.application.credentials

        credentials = await credentials_data.list_account_credentials

        for credential in credentials:
            await credentials_data.detach(credential, account_id, self, db=db)

        try:
            await (db or self.db).execute(
                """
                    DELETE FROM `accounts`
                    WHERE `account_id`=%s;
                """, account_id, hash_args=('accounts', account_id))
        except DatabaseError as e:
            raise AccountError("Failed to delete account: " + e.args[1])

    async def lookup_account(self, credential, db=None):

        credentials = self.application.credentials

        try:
            account = await credentials.get_account(credential, db=db)
        except CredentialNotFound:
            account = await self.create_account(db=db)
            await credentials.attach(credential, account, db=db)
            return account
        else:
            return account

    async def check_account_exists(self, account, db=None):
        try:
            exists = await (db or self.db).get(
                """
                    SELECT 1
                    FROM `accounts`
                    WHERE `account_id`=%s;
                """, account)
        except DatabaseError as e:
            raise AccountError("Failed to get account info: " + e.args[1])
        else:
            return bool(exists)

    async def get_account_info(self, account, db=None):
        """
        Returns account information
        """

        try:
            info = await (db or self.db).get(
                """
                    SELECT `account_info`
                    FROM `accounts`
                    WHERE `account_id`=%s;
                """, account)
        except DatabaseError as e:
            raise AccountError("Failed to get account info: " + e.args[1])
        else:

            if not info:
                return None

            return info["account_info"]

    async def update_account_info(self, account, account_info, db=None):
        """
        Updates account information
        """

        if not isinstance(account_info, dict):
            raise AccountError("Should be a dict")

        value = await self.get_account_info(account, db=db)
        update(value, account_info)

        try:
            await (db or self.db).execute(
                """
                    UPDATE `accounts`
                    SET `account_info`=%s
                    WHERE `account_id`=%s;
                """, ujson.dumps(value), account)
        except DatabaseError as e:
            raise AccountError("Failed to update account info: " + e.args[1])

    async def create_account(self, db=None):
        """
        Creates a new account in the system.

        :return: A new account ID.
        """

        try:
            result = await (db or self.db).insert(
                """
                    INSERT INTO `accounts`
                    (`account_info`)
                    VALUES (%s);
                """, ujson.dumps({}))
        except DatabaseError as e:
            raise AccountError("Failed to create account: " + e.args[1])

        self.application.monitor_rate("accounts", "created")

        return str(result)

    async def resolve_conflict(self, resolve_token, method_to_resolve, args, env=None):
        """
        Resolves an existing conflict. Please see `__merge_accounts__` for more information.

        :param resolve_token: A token given to the user when conflict happened, gives right to resolve the conflict.
        :param method_to_resolve: A chosen method how to resolve the conflict.
        :param args: Other arguments
        :return:
        """

        try:
            resolve_with = args["resolve_with"]
            requested_scopes = access.parse_scopes(args["scopes"])
            attach_to = args.get("attach_to")
        except KeyError:
            raise AuthenticationError(
                400,
                "missing_argument",
                info="Some argument is missing.")

        tokens = self.application.tokens

        credential = resolve_token.name
        gamespace = resolve_token.get(AccessToken.GAMESPACE)

        async def _multiple_accounts_attached():
            select_option = args.get("resolve_with", None)

            credentials = self.application.credentials
            accounts = await credentials.list_accounts(credential, db=db)

            try:
                accounts.remove(select_option)
            except ValueError:
                raise AuthenticationError(
                    400,
                    "cannot_resolve_conflict",
                    info="No such account to select: '{0}'.".format(select_option))

            for other_account in accounts:
                await credentials.detach(
                    credential,
                    other_account,
                    accounts_data=self,
                    db=db)

            return select_option

        async def _merge_required():
            attach_to_token = AccessToken(attach_to)

            if not (await tokens.validate(attach_to_token)):
                raise AuthenticationError(
                    403,
                    "attach_to_token_invalid",
                    info="Token attach to is not valid")

            res = await self.__merge_accounts__(
                attach_to_token,
                credential,
                resolve_with,
                gamespace,
                db=db)

            return res

        resolve_methods = {
            "merge_required": _merge_required,
            "multiple_accounts_attached": _multiple_accounts_attached
        }

        if method_to_resolve not in resolve_methods:
            raise AuthenticationError(
                400,
                "bad_resolve_method",
                info="Resolve method unsupported: " + method_to_resolve)

        method_to_resolve = resolve_methods[method_to_resolve]

        async with self.db.acquire() as db:
            account = await method_to_resolve()
            logging.debug("Resolved with: {0}".format(account))
            result = await self.proceed_authentication(
                account,
                credential,
                gamespace,
                requested_scopes,
                args,
                env=env,
                db=db)

        return result


class AuthenticationError(Exception):
    def __init__(self, code, result, **other):
        super(AuthenticationError, self).__init__()
        self.code = code
        self.obj = other
        self.obj["result_id"] = result


class AccountError(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message
