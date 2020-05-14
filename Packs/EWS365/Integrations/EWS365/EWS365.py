import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *

import sys
import traceback
import json
import os
import hashlib
from datetime import timedelta
from io import StringIO
import logging
import warnings
import email
from requests.exceptions import ConnectionError
from collections import deque

from multiprocessing import Process
import exchangelib
from exchangelib.errors import (
    ErrorItemNotFound,
    ResponseMessageError,
    TransportError,
    RateLimitError,
    ErrorInvalidIdMalformed,
    ErrorFolderNotFound,
    ErrorMailboxStoreUnavailable,
    ErrorMailboxMoveInProgress,
    ErrorNameResolutionNoResults,
    ErrorInvalidPropertyRequest,
    ErrorIrresolvableConflict,
    MalformedResponseError,
)
from exchangelib.items import Item, Message, Contact
from exchangelib.services.common import EWSService, EWSAccountService
from exchangelib.util import create_element, add_xml_child, MNS, TNS
from exchangelib import (
    IMPERSONATION,
    DELEGATE,
    Account,
    EWSDateTime,
    EWSTimeZone,
    Configuration,
    FileAttachment,
    Version,
    Folder,
    HTMLBody,
    Body,
    ItemAttachment,
    OAUTH2,
    OAuth2AuthorizationCodeCredentials,
)
from oauthlib.oauth2 import OAuth2Token
from exchangelib.version import EXCHANGE_O365
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

# Ignore warnings print to stdout
warnings.filterwarnings("ignore")

""" Constants """

APP_NAME = "ews365"

# move results
MOVED_TO_MAILBOX = "movedToMailbox"
MOVED_TO_FOLDER = "movedToFolder"

# item types
FILE_ATTACHMENT_TYPE = "FileAttachment"
ITEM_ATTACHMENT_TYPE = "ItemAttachment"
ATTACHMENT_TYPE = "attachmentType"

TOIS_PATH = "/root/Top of Information Store/"

# context keys
ATTACHMENT_ID = "attachmentId"
ATTACHMENT_ORIGINAL_ITEM_ID = "originalItemId"
NEW_ITEM_ID = "newItemId"
MESSAGE_ID = "messageId"
ITEM_ID = "itemId"
ACTION = "action"
MAILBOX = "mailbox"
MAILBOX_ID = "mailboxId"
FOLDER_ID = "id"

# context paths
CONTEXT_UPDATE_EWS_ITEM = "EWS.Items(val.{0} == obj.{0} || (val.{1} && obj.{1} && val.{1} == obj.{1}))".format(
    ITEM_ID, MESSAGE_ID
)
CONTEXT_UPDATE_EWS_ITEM_FOR_ATTACHMENT = "EWS.Items(val.{0} == obj.{1})".format(
    ITEM_ID, ATTACHMENT_ORIGINAL_ITEM_ID
)
CONTEXT_UPDATE_ITEM_ATTACHMENT = ".ItemAttachments(val.{0} == obj.{0})".format(
    ATTACHMENT_ID
)
CONTEXT_UPDATE_FILE_ATTACHMENT = ".FileAttachments(val.{0} == obj.{0})".format(
    ATTACHMENT_ID
)
CONTEXT_UPDATE_FOLDER = "EWS.Folders(val.{0} == obj.{0})".format(FOLDER_ID)

# fetch params
LAST_RUN_TIME = "lastRunTime"
LAST_RUN_IDS = "ids"
LAST_RUN_FOLDER = "folderName"
ERROR_COUNTER = "errorCounter"

# headers
ITEMS_RESULTS_HEADERS = [
    "sender",
    "subject",
    "hasAttachments",
    "datetimeReceived",
    "receivedBy",
    "author",
    "toRecipients",
    "textBody",
]

""" Classes """


class EWSClient:
    def __init__(
        self,
        default_target_mailbox,
        client_id,
        client_secret,
        tenant_id,
        folder="Inbox",
        is_public_folder=False,
        impersonation=False,
        request_timeout="120",
        mark_as_read=False,
        max_fetch="50",
        self_deployed=True,
        insecure=True,
        proxy=False,
        **kwargs,
    ):
        """
        Client used to communicate with EWS
        :param default_target_mailbox: Email address from which to fetch incidents
        :param client_id: Application client ID
        :param client_secret: Application client secret
        :param folder: Name of the folder from which to fetch incidents
        :param is_public_folder: Public Folder flag
        :param impersonation: Has impersonation rights flag
        :param request_timeout: Timeout (in seconds) for HTTP requests to Exchange Server
        :param mark_as_read: Mark fetched emails as read
        :param max_fetch: Max incidents per fetch
        :param insecure: Trust any certificate (not secure)
        """
        BaseProtocol.TIMEOUT = int(request_timeout)
        self.ews_server = "https://outlook.office365.com/EWS/Exchange.asmx/"
        self.ms_client = MicrosoftClient(
            tenant_id=tenant_id,
            auth_id=client_id,
            enc_key=client_secret,
            app_name=APP_NAME,
            base_url=self.ews_server,
            verify=insecure,
            proxy=proxy,
            self_deployed=self_deployed,
            scope="https://outlook.office.com/.default",
        )
        self.folder_name = folder
        self.is_public_folder = is_public_folder
        self.access_type = IMPERSONATION if impersonation else DELEGATE
        self.mark_as_read = mark_as_read
        self.max_fetch = min(50, int(max_fetch))
        self.last_run_ids_queue_size = 500
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_email = default_target_mailbox
        self.config = self.__prepare(insecure)
        self.protocol = BaseProtocol(self.config)

    def __prepare(self, insecure):
        if insecure:
            BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter

        access_token = self.ms_client.get_access_token()
        oauth2_token = OAuth2Token({"access_token": access_token})
        self.credentials = credentials = OAuth2AuthorizationCodeCredentials(
            client_id=self.client_id,
            client_secret=self.client_secret,
            access_token=oauth2_token,
        )
        config_args = {
            "credentials": credentials,
            "auth_type": OAUTH2,
            "version": Version(EXCHANGE_O365),
            "service_endpoint": "https://outlook.office365.com/EWS/Exchange.asmx",
        }

        return Configuration(**config_args)

    def get_account(self, target_mailbox=None, access_type=None):
        if not target_mailbox:
            target_mailbox = self.account_email
        if not access_type:
            access_type = self.access_type
        return Account(
            primary_smtp_address=target_mailbox,
            autodiscover=False,
            config=self.config,
            access_type=access_type,
        )

    def get_items_from_mailbox(self, account, item_ids):
        # allow user to pass target_mailbox as account
        if isinstance(account, str):
            account = self.get_account(account)
        if type(item_ids) is not list:
            item_ids = [item_ids]
        items = [Item(id=x) for x in item_ids]
        result = list(account.fetch(ids=items))
        result = [x for x in result if not isinstance(x, ErrorItemNotFound)]
        if len(result) != len(item_ids):
            raise Exception(
                "One or more items were not found. Check the input item ids"
            )
        return result

    def get_item_from_mailbox(self, account, item_id):
        result = self.get_items_from_mailbox(account, [item_id])
        if len(result) == 0:
            raise Exception(f"ItemId {str(item_id)} not found")
        return result[0]

    def get_attachments_for_item(self, item_id, account, attachment_ids=None):
        item = self.get_item_from_mailbox(account, item_id)
        attachments = []
        attachment_ids = argToList(attachment_ids)
        if item:
            if item.attachments:
                for attachment in item.attachments:
                    if (
                        attachment_ids
                        and attachment.attachment_id.id not in attachment_ids
                    ):
                        continue
                    attachments.append(attachment)

        else:
            raise Exception("Message item not found: " + item_id)

        if attachment_ids and len(attachments) < len(attachment_ids):
            raise Exception(
                "Some attachment id did not found for message:" + str(attachment_ids)
            )

        return attachments

    def is_default_folder(self, folder_path, is_public):
        if is_public is not None:
            return is_public

        if folder_path == self.folder_name:
            return self.is_public_folder

        return False

    def get_folder_by_path(self, path, account=None, is_public=False):
        if account is None:
            account = self.get_account()
        # handle exchange folder id
        if len(path) == 120:
            folders_map = account.root._folders_map
            if path in folders_map:
                return account.root._folders_map[path]

        if is_public:
            folder_result = account.public_folders_root
        elif path == "AllItems":
            folder_result = account.root
        else:
            folder_result = account.inbox.parent  # Top of Information Store
        path = path.replace("/", "\\")
        path = path.split("\\")
        for sub_folder_name in path:
            folder_filter_by_name = [
                x
                for x in folder_result.children
                if x.name.lower() == sub_folder_name.lower()
            ]
            if len(folder_filter_by_name) == 0:
                raise Exception(f"No such folder {path}")
            folder_result = folder_filter_by_name[0]

        return folder_result


class MarkAsJunk(EWSAccountService):
    SERVICE_NAME = "MarkAsJunk"

    def call(self, item_id, move_item):
        elements = list(
            self._get_elements(
                payload=self.get_payload(item_id=item_id, move_item=move_item)
            )
        )
        for element in elements:
            if isinstance(element, ResponseMessageError):
                return element.message
        return "Success"

    def get_payload(self, item_id, move_item):
        junk = create_element(
            f"m:{self.SERVICE_NAME}",
            IsJunk="true",
            MoveItem="true" if move_item else "false",
        )

        items_list = create_element("m:ItemIds")
        item_element = create_element("t:ItemId", Id=item_id)
        items_list.append(item_element)
        junk.append(items_list)

        return junk


class GetSearchableMailboxes(EWSService):
    SERVICE_NAME = "GetSearchableMailboxes"
    element_container_name = f"{MNS}SearchableMailboxes"

    @staticmethod
    def parse_element(element):
        return {
            MAILBOX: element.find(f"{TNS}PrimarySmtpAddress").text
            if element.find(f"{TNS}PrimarySmtpAddress") is not None
            else None,
            MAILBOX_ID: element.find(f"{TNS}ReferenceId").text
            if element.find(f"{TNS}ReferenceId") is not None
            else None,
            "displayName": element.find(f"{TNS}DisplayName").text
            if element.find(f"{TNS}DisplayName") is not None
            else None,
            "isExternal": element.find(f"{TNS}IsExternalMailbox").text
            if element.find(f"{TNS}IsExternalMailbox") is not None
            else None,
            "externalEmailAddress": element.find(f"{TNS}ExternalEmailAddress").text
            if element.find(f"{TNS}ExternalEmailAddress") is not None
            else None,
        }

    def call(self):
        elements = self._get_elements(payload=self.get_payload())
        return [self.parse_element(x) for x in elements]

    def get_payload(self):
        element = create_element(f"m:{self.SERVICE_NAME}",)
        return element


class SearchMailboxes(EWSService):
    SERVICE_NAME = "SearchMailboxes"
    element_container_name = f"{MNS}SearchMailboxesResult/{TNS}Items"

    @staticmethod
    def parse_element(element):
        to_recipients = element.find(f"{TNS}ToRecipients")
        if to_recipients:
            to_recipients = [x.text if x is not None else None for x in to_recipients]

        result = {
            ITEM_ID: element.find(f"{TNS}Id").attrib["Id"]
            if element.find(f"{TNS}Id") is not None
            else None,
            MAILBOX: element.find(f"{TNS}Mailbox/{TNS}PrimarySmtpAddress").text
            if element.find(f"{TNS}Mailbox/{TNS}PrimarySmtpAddress") is not None
            else None,
            "subject": element.find(f"{TNS}Subject").text
            if element.find(f"{TNS}Subject") is not None
            else None,
            "toRecipients": to_recipients,
            "sender": element.find(f"{TNS}Sender").text
            if element.find(f"{TNS}Sender") is not None
            else None,
            "hasAttachments": element.find(f"{TNS}HasAttachment").text
            if element.find(f"{TNS}HasAttachment") is not None
            else None,
            "datetimeSent": element.find(f"{TNS}SentTime").text
            if element.find(f"{TNS}SentTime") is not None
            else None,
            "datetimeReceived": element.find(f"{TNS}ReceivedTime").text
            if element.find(f"{TNS}ReceivedTime") is not None
            else None,
        }

        return result

    def call(self, query, mailboxes):
        elements = list(self._get_elements(payload=self.get_payload(query, mailboxes)))
        return [self.parse_element(x) for x in elements]

    def get_payload(self, query, mailboxes):
        def get_mailbox_search_scope(mailbox_id):
            mailbox_search_scope = create_element("t:MailboxSearchScope")
            add_xml_child(mailbox_search_scope, "t:Mailbox", mailbox_id)
            add_xml_child(mailbox_search_scope, "t:SearchScope", "All")
            return mailbox_search_scope

        mailbox_query_element = create_element("t:MailboxQuery")
        add_xml_child(mailbox_query_element, "t:Query", query)
        mailboxes_scopes = []
        for mailbox in mailboxes:
            mailboxes_scopes.append(get_mailbox_search_scope(mailbox))
        add_xml_child(mailbox_query_element, "t:MailboxSearchScopes", mailboxes_scopes)

        element = create_element(f"m:{self.SERVICE_NAME}")
        add_xml_child(element, "m:SearchQueries", mailbox_query_element)
        add_xml_child(element, "m:ResultType", "PreviewOnly")

        return element


class ExpandGroup(EWSService):
    SERVICE_NAME = "ExpandDL"
    element_container_name = f"{MNS}DLExpansion"

    @staticmethod
    def parse_element(element):
        return {
            MAILBOX: element.find(f"{TNS}EmailAddress").text
            if element.find(f"{TNS}EmailAddress") is not None
            else None,
            "displayName": element.find(f"{TNS}Name").text
            if element.find(f"{TNS}Name") is not None
            else None,
            "mailboxType": element.find(f"{TNS}MailboxType").text
            if element.find(f"{TNS}MailboxType") is not None
            else None,
        }

    def call(self, email_address, recursive_expansion=False):
        try:
            if recursive_expansion == "True":
                group_members = {}  # type: dict
                self.expand_group_recursive(email_address, group_members)
                return list(group_members.values())
            else:
                return self.expand_group(email_address)
        except ErrorNameResolutionNoResults:
            demisto.results("No results were found.")
            sys.exit()

    def get_payload(self, email_address):
        element = create_element(f"m:{self.SERVICE_NAME}")
        mailbox_element = create_element("m:Mailbox")
        add_xml_child(mailbox_element, "t:EmailAddress", email_address)
        element.append(mailbox_element)
        return element

    def expand_group(self, email_address):
        elements = self._get_elements(payload=self.get_payload(email_address))
        return [self.parse_element(x) for x in elements]

    def expand_group_recursive(self, email_address, non_dl_emails, dl_emails=set()):
        if email_address in non_dl_emails or email_address in dl_emails:
            return None
        dl_emails.add(email_address)

        for member in self.expand_group(email_address):
            if (
                member["mailboxType"] == "PublicDL"
                or member["mailboxType"] == "PrivateDL"
            ):
                self.expand_group_recursive(member["mailbox"], non_dl_emails, dl_emails)
            else:
                if member["mailbox"] not in non_dl_emails:
                    non_dl_emails[member["mailbox"]] = member


# If you are modifying this probably also need to modify in other files
def exchangelib_cleanup():
    key_protocols = list(exchangelib.protocol.CachingProtocol._protocol_cache.items())
    try:
        exchangelib.close_connections()
    except Exception as ex:
        demisto.error("Error was found in exchangelib cleanup, ignoring: {}".format(ex))
    for key, protocol in key_protocols:
        try:
            if "thread_pool" in protocol.__dict__:
                demisto.debug(
                    "terminating thread pool key{} id: {}".format(
                        key, id(protocol.thread_pool)
                    )
                )
                protocol.thread_pool.terminate()
                del protocol.__dict__["thread_pool"]
            else:
                demisto.info(
                    "Thread pool not found (ignoring terminate) in protcol dict: {}".format(
                        dir(protocol.__dict__)
                    )
                )
        except Exception as ex:
            demisto.error("Error with thread_pool.terminate, ignoring: {}".format(ex))


""" LOGGING """

log_stream = None
log_handler = None


def start_logging():
    global log_stream
    global log_handler
    logging.raiseExceptions = False
    if log_stream is None:
        log_stream = StringIO()
        log_handler = logging.StreamHandler(stream=log_stream)
        log_handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
        logger = logging.getLogger()
        logger.addHandler(log_handler)
        logger.setLevel(logging.DEBUG)


""" Helper Functions """


def get_attachment_name(attachment_name):
    if attachment_name is None or attachment_name == "":
        return "demisto_untitled_attachment"
    return attachment_name


def get_entry_for_object(title, context_key, obj, headers=None):
    if len(obj) == 0:
        return "There is no output results"
    if headers and isinstance(obj, dict):
        headers = list(set(headers).intersection(set(obj.keys())))

    return {
        "Type": entryTypes["note"],
        "Contents": obj,
        "ContentsFormat": formats["json"],
        "ReadableContentsFormat": formats["markdown"],
        "HumanReadable": tableToMarkdown(title, obj, headers),
        "EntryContext": {context_key: obj},
    }


def prepare_args(d):
    d = dict((k.replace("-", "_"), v) for k, v in list(d.items()))
    if "is_public" in d:
        d["is_public"] = d["is_public"] == "True"
    return d


def get_limited_number_of_messages_from_qs(qs, limit):
    count = 0
    results = []
    for item in qs:
        if count == limit:
            break
        if isinstance(item, Message):
            count += 1
            results.append(item)
    return results


def keys_to_camel_case(value):
    def str_to_camel_case(snake_str):
        components = snake_str.split("_")
        return components[0] + "".join(x.title() for x in components[1:])

    if value is None:
        return None
    if isinstance(value, (list, set)):
        return list(map(keys_to_camel_case, value))
    if isinstance(value, dict):
        return dict(
            (
                keys_to_camel_case(k),
                keys_to_camel_case(v) if isinstance(v, (list, dict)) else v,
            )
            for (k, v) in list(value.items())
        )

    return str_to_camel_case(value)


def get_last_run(client: EWSClient):
    last_run = demisto.getLastRun()
    if not last_run or last_run.get(LAST_RUN_FOLDER) != client.folder_name:
        last_run = {
            LAST_RUN_TIME: None,
            LAST_RUN_FOLDER: client.folder_name,
            LAST_RUN_IDS: [],
        }
    if LAST_RUN_TIME in last_run and last_run[LAST_RUN_TIME] is not None:
        last_run[LAST_RUN_TIME] = EWSDateTime.from_string(last_run[LAST_RUN_TIME])

    # In case we have existing last_run data
    if last_run.get(LAST_RUN_IDS) is None:
        last_run[LAST_RUN_IDS] = []

    return last_run


""" Command Functions """


def get_expanded_group(client, email_address, recursive_expansion=False):
    group_members = ExpandGroup(protocol=client.protocol).call(
        email_address, recursive_expansion
    )
    group_details = {"name": email_address, "members": group_members}
    output = {"EWS.ExpandGroup": group_details}
    readable_output = tableToMarkdown("Group Members", group_members)
    return readable_output, output, group_details


def get_searchable_mailboxes(client: EWSClient):
    searchable_mailboxes = GetSearchableMailboxes(protocol=client.protocol).call()
    readable_output = tableToMarkdown("Searchable mailboxes", searchable_mailboxes)
    output = {"EWS.Mailboxes": searchable_mailboxes}
    return readable_output, output, searchable_mailboxes


def search_mailboxes(
    client: EWSClient,
    filter,
    limit=100,
    mailbox_search_scope=None,
    email_addresses=None,
):
    mailbox_ids = []
    limit = int(limit)
    protocol = client.protocol
    if mailbox_search_scope is not None and email_addresses is not None:
        raise Exception(
            "Use one of the arguments - mailbox-search-scope or email-addresses, not both"
        )
    if email_addresses:
        email_addresses = email_addresses.split(",")
        all_mailboxes = GetSearchableMailboxes(protocol=protocol).call()
        for email_address in email_addresses:
            for mailbox in all_mailboxes:
                if (
                    MAILBOX in mailbox
                    and email_address.lower() == mailbox[MAILBOX].lower()
                ):
                    mailbox_ids.append(mailbox[MAILBOX_ID])
        if len(mailbox_ids) == 0:
            raise Exception(
                "No searchable mailboxes were found for the provided email addresses."
            )
    elif mailbox_search_scope:
        mailbox_ids = (
            mailbox_search_scope
            if type(mailbox_search_scope) is list
            else [mailbox_search_scope]
        )
    else:
        entry = GetSearchableMailboxes(protocol=protocol).call()
        mailboxes = [x for x in entry if MAILBOX_ID in list(x.keys())]
        mailbox_ids = [x[MAILBOX_ID] for x in mailboxes]

    try:
        search_results = SearchMailboxes(protocol=protocol).call(filter, mailbox_ids)
        search_results = search_results[:limit]
    except TransportError as e:
        if "ItemCount>0<" in str(e):
            return "No results for search query: " + filter
        else:
            raise e

    readable_output = tableToMarkdown("Search mailboxes results", search_results)
    output = {CONTEXT_UPDATE_EWS_ITEM: search_results}
    return readable_output, output, search_results


def fetch_last_emails(
    client: EWSClient, folder_name="Inbox", since_datetime=None, exclude_ids=None
):
    qs = client.get_folder_by_path(folder_name, is_public=client.is_public_folder)
    if since_datetime:
        qs = qs.filter(datetime_received__gte=since_datetime)
    else:
        last_10_min = EWSDateTime.now(tz=EWSTimeZone.timezone("UTC")) - timedelta(
            minutes=10
        )
        qs = qs.filter(datetime_received__gte=last_10_min)
    qs = qs.filter().only(*[x.name for x in Message.FIELDS])
    qs = qs.filter().order_by("datetime_received")

    result = qs.all()
    result = [x for x in result if isinstance(x, Message)]
    if exclude_ids and len(exclude_ids) > 0:
        exclude_ids = set(exclude_ids)
        result = [x for x in result if x.message_id not in exclude_ids]
    return result


def email_ec(item):
    return {
        "CC": None
        if not item.cc_recipients
        else [mailbox.email_address for mailbox in item.cc_recipients],
        "BCC": None
        if not item.bcc_recipients
        else [mailbox.email_address for mailbox in item.bcc_recipients],
        "To": None
        if not item.to_recipients
        else [mailbox.email_address for mailbox in item.to_recipients],
        "From": item.author.email_address,
        "Subject": item.subject,
        "Text": item.text_body,
        "HTML": item.body,
        "HeadersMap": {header.name: header.value for header in item.headers},
    }


def parse_item_as_dict(item, email_address, camel_case=False, compact_fields=False):
    def parse_object_as_dict(object):
        raw_dict = {}
        if object is not None:
            for field in object.FIELDS:
                raw_dict[field.name] = getattr(object, field.name, None)
        return raw_dict

    def parse_attachment_as_raw_json(attachment):
        raw_dict = parse_object_as_dict(attachment)
        if raw_dict["attachment_id"]:
            raw_dict["attachment_id"] = parse_object_as_dict(raw_dict["attachment_id"])
        if raw_dict["last_modified_time"]:
            raw_dict["last_modified_time"] = raw_dict["last_modified_time"].ewsformat()
        return raw_dict

    def parse_folder_as_json(folder):
        raw_dict = parse_object_as_dict(folder)
        if "parent_folder_id" in raw_dict:
            raw_dict["parent_folder_id"] = parse_folder_as_json(
                raw_dict["parent_folder_id"]
            )
        if "effective_rights" in raw_dict:
            raw_dict["effective_rights"] = parse_object_as_dict(
                raw_dict["effective_rights"]
            )
        return raw_dict

    raw_dict = {}
    for field, value in list(item.__dict__.items()):
        if type(value) in [str, str, int, float, bool, Body, HTMLBody, None]:
            try:
                if isinstance(value, str):
                    value.encode("utf-8")  # type: ignore
                raw_dict[field] = value
            except Exception:
                pass

    if getattr(item, "attachments", None):
        raw_dict["attachments"] = [
            parse_attachment_as_dict(item.item_id, x) for x in item.attachments
        ]

    for time_field in [
        "datetime_sent",
        "datetime_created",
        "datetime_received",
        "last_modified_time",
        "reminder_due_by",
    ]:
        value = getattr(item, time_field, None)
        if value:
            raw_dict[time_field] = value.ewsformat()

    for dict_field in [
        "effective_rights",
        "parent_folder_id",
        "conversation_id",
        "author",
        "extern_id",
        "received_by",
        "received_representing",
        "reply_to",
        "sender",
        "folder",
    ]:
        value = getattr(item, dict_field, None)
        if value:
            raw_dict[dict_field] = parse_object_as_dict(value)

    for list_dict_field in ["headers", "cc_recipients", "to_recipients"]:
        value = getattr(item, list_dict_field, None)
        if value:
            raw_dict[list_dict_field] = [parse_object_as_dict(x) for x in value]

    if getattr(item, "folder", None):
        raw_dict["folder"] = parse_folder_as_json(item.folder)
        folder_path = (
            item.folder.absolute[len(TOIS_PATH) :]
            if item.folder.absolute.startswith(TOIS_PATH)
            else item.folder.absolute
        )
        raw_dict["folder_path"] = folder_path

    if compact_fields:
        new_dict = {}
        # noinspection PyListCreation
        fields_list = [
            "datetime_created",
            "datetime_received",
            "datetime_sent",
            "sender",
            "has_attachments",
            "importance",
            "message_id",
            "last_modified_time",
            "size",
            "subject",
            "text_body",
            "headers",
            "body",
            "folder_path",
            "is_read",
        ]

        if "id" in raw_dict:
            new_dict["item_id"] = raw_dict["id"]
            fields_list.append("item_id")

        for field in fields_list:
            if field in raw_dict:
                new_dict[field] = raw_dict.get(field)
        for field in ["received_by", "author", "sender"]:
            if field in raw_dict:
                new_dict[field] = raw_dict.get(field, {}).get("email_address")
        for field in ["to_recipients"]:
            if field in raw_dict:
                new_dict[field] = [x.get("email_address") for x in raw_dict[field]]
        attachments = raw_dict.get("attachments")
        if attachments and len(attachments) > 0:
            file_attachments = [
                x for x in attachments if x[ATTACHMENT_TYPE] == FILE_ATTACHMENT_TYPE
            ]
            if len(file_attachments) > 0:
                new_dict["FileAttachments"] = file_attachments
            item_attachments = [
                x for x in attachments if x[ATTACHMENT_TYPE] == ITEM_ATTACHMENT_TYPE
            ]
            if len(item_attachments) > 0:
                new_dict["ItemAttachments"] = item_attachments

        raw_dict = new_dict

    if camel_case:
        raw_dict = keys_to_camel_case(raw_dict)

    if email_address:
        raw_dict[MAILBOX] = email_address
    return raw_dict


def parse_incident_from_item(client: EWSClient, item, is_fetch=False):
    incident = {}
    labels = []

    try:
        incident["details"] = item.text_body or item.body
    except AttributeError:
        incident["details"] = item.body
    incident["name"] = item.subject
    labels.append({"type": "Email/subject", "value": item.subject})
    incident["occurred"] = item.datetime_created.ewsformat()

    # handle recipients
    if item.to_recipients:
        for recipient in item.to_recipients:
            labels.append({"type": "Email", "value": recipient.email_address})

    # handle cc
    if item.cc_recipients:
        for recipient in item.cc_recipients:
            labels.append({"type": "Email/cc", "value": recipient.email_address})
    # handle email from
    if item.sender:
        labels.append({"type": "Email/from", "value": item.sender.email_address})

    # email format
    email_format = ""
    try:
        if item.text_body:
            labels.append({"type": "Email/text", "value": item.text_body})
            email_format = "text"
    except AttributeError:
        pass
    if item.body:
        labels.append({"type": "Email/html", "value": item.body})
        email_format = "HTML"
    labels.append({"type": "Email/format", "value": email_format})

    # handle attachments
    if item.attachments:
        incident["attachment"] = []
        for attachment in item.attachments:
            file_result = None
            label_attachment_type = None
            label_attachment_id_type = None
            if isinstance(attachment, FileAttachment):
                try:
                    if attachment.content:
                        # file attachment
                        label_attachment_type = "attachments"
                        label_attachment_id_type = "attachmentId"

                        # save the attachment
                        file_name = get_attachment_name(attachment.name)
                        file_result = fileResult(file_name, attachment.content)

                        # check for error
                        if file_result["Type"] == entryTypes["error"]:
                            demisto.error(file_result["Contents"])
                            raise Exception(file_result["Contents"])

                        # save attachment to incident
                        incident["attachment"].append(
                            {
                                "path": file_result["FileID"],
                                "name": get_attachment_name(attachment.name),
                            }
                        )
                except TypeError as e:
                    if e.message != "must be string or buffer, not None":
                        raise
                    continue
            else:
                # other item attachment
                label_attachment_type = "attachmentItems"
                label_attachment_id_type = "attachmentItemsId"

                # save the attachment
                if attachment.item.mime_content:
                    attached_email = email.message_from_string(
                        attachment.item.mime_content
                    )
                    if attachment.item.headers:
                        attached_email_headers = [
                            (h, " ".join(map(str.strip, v.split("\r\n"))))
                            for (h, v) in list(attached_email.items())
                        ]
                        for header in attachment.item.headers:
                            if (
                                (header.name, header.value)
                                not in attached_email_headers
                                and header.name != "Content-Type"
                            ):
                                attached_email.add_header(header.name, header.value)

                    file_result = fileResult(
                        get_attachment_name(attachment.name) + ".eml",
                        attached_email.as_string(),
                    )

                if file_result:
                    # check for error
                    if file_result["Type"] == entryTypes["error"]:
                        demisto.error(file_result["Contents"])
                        raise Exception(file_result["Contents"])

                    # save attachment to incident
                    incident["attachment"].append(
                        {
                            "path": file_result["FileID"],
                            "name": get_attachment_name(attachment.name) + ".eml",
                        }
                    )

            labels.append(
                {
                    "type": label_attachment_type,
                    "value": get_attachment_name(attachment.name),
                }
            )
            labels.append(
                {"type": label_attachment_id_type, "value": attachment.attachment_id.id}
            )

    # handle headers
    if item.headers:
        headers = []
        for header in item.headers:
            labels.append(
                {
                    "type": "Email/Header/{}".format(header.name),
                    "value": str(header.value),
                }
            )
            headers.append("{}: {}".format(header.name, header.value))
        labels.append({"type": "Email/headers", "value": "\r\n".join(headers)})

    # handle item id
    if item.message_id:
        labels.append({"type": "Email/MessageId", "value": str(item.message_id)})

    if item.id:
        labels.append({"type": "Email/ID", "value": item.id})
        labels.append({"type": "Email/itemId", "value": item.id})

    # handle conversion id
    if item.conversation_id:
        labels.append({"type": "Email/ConversionID", "value": item.conversation_id.id})

    if client.mark_as_read and is_fetch:
        item.is_read = True
        try:
            item.save()
        except ErrorIrresolvableConflict:
            time.sleep(0.5)
            item.save()

    incident["labels"] = labels
    incident["rawJSON"] = json.dumps(parse_item_as_dict(item, None), ensure_ascii=False)

    return incident


def fetch_emails_as_incidents(client: EWSClient):
    last_run = get_last_run(client)

    try:
        last_emails = fetch_last_emails(
            client,
            client.folder_name,
            last_run.get(LAST_RUN_TIME),
            last_run.get(LAST_RUN_IDS),
        )

        ids = deque(
            last_run.get(LAST_RUN_IDS, []), maxlen=client.last_run_ids_queue_size
        )
        incidents = []
        incident = {}  # type: Dict[Any, Any]
        for item in last_emails:
            if item.message_id:
                ids.append(item.message_id)
                incident = parse_incident_from_item(client, item, is_fetch=True)
                incidents.append(incident)

                if len(incidents) >= client.max_fetch:
                    break

        last_run_time = incident.get("occurred", last_run.get(LAST_RUN_TIME))
        if isinstance(last_run_time, EWSDateTime):
            last_run_time = last_run_time.ewsformat()

        new_last_run = {
            LAST_RUN_TIME: last_run_time,
            LAST_RUN_FOLDER: client.folder_name,
            LAST_RUN_IDS: list(ids),
            ERROR_COUNTER: 0,
        }

        demisto.setLastRun(new_last_run)
        return incidents

    except RateLimitError:
        if LAST_RUN_TIME in last_run:
            last_run[LAST_RUN_TIME] = last_run[LAST_RUN_TIME].ewsformat()
        if ERROR_COUNTER not in last_run:
            last_run[ERROR_COUNTER] = 0
        last_run[ERROR_COUNTER] += 1
        demisto.setLastRun(last_run)
        if last_run[ERROR_COUNTER] > 2:
            raise
        return []


def get_entry_for_file_attachment(item_id, attachment):
    entry = fileResult(get_attachment_name(attachment.name), attachment.content)
    entry["EntryContext"] = {
        CONTEXT_UPDATE_EWS_ITEM_FOR_ATTACHMENT
        + CONTEXT_UPDATE_FILE_ATTACHMENT: parse_attachment_as_dict(item_id, attachment)
    }
    return entry


def parse_attachment_as_dict(item_id, attachment):
    try:
        attachment_content = (
            attachment.content
            if isinstance(attachment, FileAttachment)
            else attachment.item.mime_content
        )
        return {
            ATTACHMENT_ORIGINAL_ITEM_ID: item_id,
            ATTACHMENT_ID: attachment.attachment_id.id,
            "attachmentName": get_attachment_name(attachment.name),
            "attachmentSHA256": hashlib.sha256(attachment_content).hexdigest()
            if attachment_content
            else None,
            "attachmentContentType": attachment.content_type,
            "attachmentContentId": attachment.content_id,
            "attachmentContentLocation": attachment.content_location,
            "attachmentSize": attachment.size,
            "attachmentLastModifiedTime": attachment.last_modified_time.ewsformat(),
            "attachmentIsInline": attachment.is_inline,
            ATTACHMENT_TYPE: FILE_ATTACHMENT_TYPE
            if isinstance(attachment, FileAttachment)
            else ITEM_ATTACHMENT_TYPE,
        }
    except TypeError as e:
        if e.message != "must be string or buffer, not None":
            raise
        return {
            ATTACHMENT_ORIGINAL_ITEM_ID: item_id,
            ATTACHMENT_ID: attachment.attachment_id.id,
            "attachmentName": get_attachment_name(attachment.name),
            "attachmentSHA256": None,
            "attachmentContentType": attachment.content_type,
            "attachmentContentId": attachment.content_id,
            "attachmentContentLocation": attachment.content_location,
            "attachmentSize": attachment.size,
            "attachmentLastModifiedTime": attachment.last_modified_time.ewsformat(),
            "attachmentIsInline": attachment.is_inline,
            ATTACHMENT_TYPE: FILE_ATTACHMENT_TYPE
            if isinstance(attachment, FileAttachment)
            else ITEM_ATTACHMENT_TYPE,
        }


def get_entry_for_item_attachment(item_id, attachment, target_email):
    item = attachment.item
    dict_result = parse_attachment_as_dict(item_id, attachment)
    dict_result.update(
        parse_item_as_dict(item, target_email, camel_case=True, compact_fields=True)
    )
    title = f'EWS get attachment got item for "{target_email}", "{get_attachment_name(attachment.name)}"'

    return get_entry_for_object(
        title,
        CONTEXT_UPDATE_EWS_ITEM_FOR_ATTACHMENT + CONTEXT_UPDATE_ITEM_ATTACHMENT,
        dict_result,
    )


def delete_attachments_for_message(
    client: EWSClient, item_id, target_mailbox=None, attachment_ids=None
):
    attachments = client.get_attachments_for_item(
        item_id, target_mailbox, attachment_ids
    )
    deleted_file_attachments = []
    deleted_item_attachments = []  # type: ignore
    for attachment in attachments:
        attachment_deleted_action = {
            ATTACHMENT_ID: attachment.attachment_id.id,
            ACTION: "deleted",
        }
        if isinstance(attachment, FileAttachment):
            deleted_file_attachments.append(attachment_deleted_action)
        else:
            deleted_item_attachments.append(attachment_deleted_action)
        attachment.detach()

    entries = []
    if len(deleted_file_attachments) > 0:
        entry = get_entry_for_object(
            "Deleted file attachments",
            "EWS.Items" + CONTEXT_UPDATE_FILE_ATTACHMENT,
            deleted_file_attachments,
        )
        entries.append(entry)
    if len(deleted_item_attachments) > 0:
        entry = get_entry_for_object(
            "Deleted item attachments",
            "EWS.Items" + CONTEXT_UPDATE_ITEM_ATTACHMENT,
            deleted_item_attachments,
        )
        entries.append(entry)

    return entries


def fetch_attachments_for_message(
    client: EWSClient, item_id, target_mailbox=None, attachment_ids=None
):
    account = client.get_account(target_mailbox)
    attachments = client.get_attachments_for_item(item_id, account, attachment_ids)
    entries = []
    for attachment in attachments:
        if isinstance(attachment, FileAttachment):
            try:
                if attachment.content:
                    entries.append(get_entry_for_file_attachment(item_id, attachment))
            except TypeError as e:
                if str(e) != "must be string or buffer, not None":
                    raise
        else:
            entries.append(
                get_entry_for_item_attachment(
                    item_id, attachment, account.primary_smtp_address
                )
            )
            if attachment.item.mime_content:
                entries.append(
                    fileResult(
                        get_attachment_name(attachment.name) + ".eml",
                        attachment.item.mime_content,
                    )
                )

    return entries


def move_item_between_mailboxes(
    client: EWSClient,
    item_id,
    destination_mailbox,
    destination_folder_path,
    source_mailbox=None,
    is_public=None,
):
    source_account = client.get_account(source_mailbox)
    destination_account = client.get_account(destination_mailbox)
    is_public = client.is_default_folder(destination_folder_path, is_public)
    destination_folder = client.get_folder_by_path(
        destination_folder_path, destination_account, is_public
    )
    item = client.get_item_from_mailbox(source_account, item_id)

    exported_items = source_account.export([item])
    destination_account.upload([(destination_folder, exported_items[0])])
    source_account.bulk_delete([item])

    move_result = {
        MOVED_TO_MAILBOX: destination_mailbox,
        MOVED_TO_FOLDER: destination_folder_path,
    }
    readable_output = "Item was moved successfully."
    output = {f"EWS.Items(val.itemId === '{item_id}')": move_result}
    return readable_output, output, move_result


def move_item(
    client: EWSClient, item_id, target_folder_path, target_mailbox=None, is_public=None
):
    account = client.get_account(target_mailbox)
    is_public = client.is_default_folder(target_folder_path, is_public)
    target_folder = client.get_folder_by_path(target_folder_path, is_public=is_public)
    item = client.get_item_from_mailbox(account, item_id)
    if isinstance(item, ErrorInvalidIdMalformed):
        raise Exception("Item not found")
    item.move(target_folder)
    move_result = {
        NEW_ITEM_ID: item.item_id,
        ITEM_ID: item_id,
        MESSAGE_ID: item.message_id,
        ACTION: "moved",
    }
    readable_output = tableToMarkdown("Moved items", move_result)
    output = {CONTEXT_UPDATE_EWS_ITEM: move_result}
    return readable_output, output, move_result


def delete_items(client: EWSClient, item_ids, delete_type, target_mailbox=None):
    deleted_items = []
    item_ids = argToList(item_ids)
    items = client.get_items_from_mailbox(target_mailbox, item_ids)
    delete_type = delete_type.lower()

    for item in items:
        item_id = item.item_id
        if delete_type == "trash":
            item.move_to_trash()
        elif delete_type == "soft":
            item.soft_delete()
        elif delete_type == "hard":
            item.delete()
        else:
            raise Exception(
                f'invalid delete type: {delete_type}. Use "trash" \\ "soft" \\ "hard"'
            )
        deleted_items.append(
            {
                ITEM_ID: item_id,
                MESSAGE_ID: item.message_id,
                ACTION: f"{delete_type}-deleted",
            }
        )

    readable_output = tableToMarkdown(
        f"Deleted items ({delete_type} delete type)", deleted_items
    )
    output = {CONTEXT_UPDATE_EWS_ITEM: deleted_items}
    return readable_output, output, deleted_items


def search_items_in_mailbox(
    client: EWSClient,
    query=None,
    message_id=None,
    folder_path="",
    limit=100,
    target_mailbox=None,
    is_public=None,
    selected_fields="all",
):
    if not query and not message_id:
        return_error("Missing required argument. Provide query or message-id")

    if message_id and message_id[0] != "<" and message_id[-1] != ">":
        message_id = "<{}>".format(message_id)

    account = client.get_account(target_mailbox)
    limit = int(limit)
    if folder_path.lower() == "inbox":
        folders = [account.inbox]
    elif folder_path:
        is_public = client.is_default_folder(folder_path, is_public)
        folders = [client.get_folder_by_path(folder_path, account, is_public)]
    else:
        folders = account.inbox.parent.walk()  # pylint: disable=E1101

    items = []  # type: ignore
    selected_all_fields = selected_fields == "all"

    if selected_all_fields:
        restricted_fields = list([x.name for x in Message.FIELDS])  # type: ignore
    else:
        restricted_fields = set(argToList(selected_fields))  # type: ignore
        restricted_fields.update(["id", "message_id"])  # type: ignore

    for folder in folders:
        if Message not in folder.supported_item_models:
            continue
        if query:
            items_qs = folder.filter(query).only(*restricted_fields)
        else:
            items_qs = folder.filter(message_id=message_id).only(*restricted_fields)
        items += get_limited_number_of_messages_from_qs(items_qs, limit)
        if len(items) >= limit:
            break

    items = items[:limit]
    searched_items_result = [
        parse_item_as_dict(
            item,
            account.primary_smtp_address,
            camel_case=True,
            compact_fields=selected_all_fields,
        )
        for item in items
    ]

    if not selected_all_fields:
        searched_items_result = [
            {k: v for (k, v) in i.items() if k in keys_to_camel_case(restricted_fields)}
            for i in searched_items_result
        ]

        for item in searched_items_result:
            item["itemId"] = item.pop("id", "")

    readable_output = tableToMarkdown(
        "Searched items",
        searched_items_result,
        headers=ITEMS_RESULTS_HEADERS if selected_all_fields else None,
    )
    output = {CONTEXT_UPDATE_EWS_ITEM: searched_items_result}
    return readable_output, output, searched_items_result


def get_out_of_office_state(client: EWSClient, target_mailbox=None):
    account = client.get_account(target_mailbox)
    oof = account.oof_settings
    oof_dict = {
        "state": oof.state,  # pylint: disable=E1101
        "externalAudience": getattr(oof, "external_audience", None),
        "start": oof.start.ewsformat() if oof.start else None,  # pylint: disable=E1101
        "end": oof.end.ewsformat() if oof.end else None,  # pylint: disable=E1101
        "internalReply": getattr(oof, "internal_replay", None),
        "externalReply": getattr(oof, "external_replay", None),
        MAILBOX: account.primary_smtp_address,
    }
    readable_output = tableToMarkdown(
        f"Out of office state for {account.primary_smtp_address}", oof_dict
    )
    output = {f"Account.Email(val.Address == obj.{MAILBOX}).OutOfOffice": oof_dict}
    return readable_output, output, oof_dict


def recover_soft_delete_item(
    client: EWSClient,
    message_ids,
    target_folder_path="Inbox",
    target_mailbox=None,
    is_public=None,
):
    account = client.get_account(target_mailbox)
    is_public = client.is_default_folder(target_folder_path, is_public)
    target_folder = client.get_folder_by_path(target_folder_path, account, is_public)
    recovered_messages = []
    if type(message_ids) != list:
        message_ids = message_ids.split(",")
    items_to_recover = account.recoverable_items_deletions.filter(  # pylint: disable=E1101
        message_id__in=message_ids
    ).all()  # pylint: disable=E1101
    if len(items_to_recover) != len(message_ids):
        raise Exception("Some message ids are missing in recoverable items directory")
    for item in items_to_recover:
        item.move(target_folder)
        recovered_messages.append(
            {ITEM_ID: item.item_id, MESSAGE_ID: item.message_id, ACTION: "recovered"}
        )
    readable_output = tableToMarkdown("Recovered messages", recovered_messages)
    output = {CONTEXT_UPDATE_EWS_ITEM: recovered_messages}
    return readable_output, output, recovered_messages


def get_contacts(client: EWSClient, limit, target_mailbox=None):
    def parse_physical_address(address):
        result = {}
        for attr in ["city", "country", "label", "state", "street", "zipcode"]:
            result[attr] = getattr(address, attr, None)
        return result

    def parse_phone_number(phone_number):
        result = {}
        for attr in ["label", "phone_number"]:
            result[attr] = getattr(phone_number, attr, None)
        return result

    def parse_contact(contact):
        contact_dict = dict(
            (k, v if not isinstance(v, EWSDateTime) else v.ewsformat())
            for k, v in list(contact.__dict__.items())
            if isinstance(v, str) or isinstance(v, EWSDateTime)
        )
        if isinstance(contact, Contact) and contact.physical_addresses:
            contact_dict["physical_addresses"] = list(
                map(parse_physical_address, contact.physical_addresses)
            )
        if isinstance(contact, Contact) and contact.phone_numbers:
            contact_dict["phone_numbers"] = list(
                map(parse_phone_number, contact.phone_numbers)
            )
        if (
            isinstance(contact, Contact)
            and contact.email_addresses
            and len(contact.email_addresses) > 0
        ):
            contact_dict["emailAddresses"] = [x.email for x in contact.email_addresses]
        contact_dict = keys_to_camel_case(contact_dict)
        contact_dict = dict((k, v) for k, v in list(contact_dict.items()) if v)
        del contact_dict["mimeContent"]
        contact_dict["originMailbox"] = target_mailbox
        return contact_dict

    account = client.get_account(target_mailbox)
    contacts = []

    for contact in account.contacts.all()[: int(limit)]:  # pylint: disable=E1101
        contacts.append(parse_contact(contact))
    readable_output = tableToMarkdown(f"Email contacts for {target_mailbox}", contacts)
    output = {"Account.Email(val.Address == obj.originMailbox).EwsContacts": contacts}
    return readable_output, output, contacts


def create_folder(client: EWSClient, new_folder_name, folder_path, target_mailbox=None):
    full_path = os.path.join(folder_path, new_folder_name)
    try:
        if client.get_folder_by_path(full_path):
            return f"Folder {full_path} already exists"
    except Exception:
        pass
    parent_folder = client.get_folder_by_path(folder_path)
    f = Folder(parent=parent_folder, name=new_folder_name)
    f.save()
    client.get_folder_by_path(full_path)
    return f"Folder {full_path} created successfully"


def find_folders(client: EWSClient, target_mailbox=None):
    account = client.get_account(target_mailbox)
    root = account.root
    if client.is_public_folder:
        root = account.public_folders_root  # todo: search online
    folders = []
    for f in root.walk():  # pylint: disable=E1101
        folder = folder_to_context_entry(f)
        folders.append(folder)
    folders_tree = root.tree()  # pylint: disable=E1101
    readable_output = folders_tree
    output = {"EWS.Folders(val.id == obj.id)": folders}
    return readable_output, output, folders


def mark_item_as_junk(client: EWSClient, item_id, move_items, target_mailbox=None):
    account = client.get_account(target_mailbox)
    move_items = move_items.lower() == "yes"
    ews_result = MarkAsJunk(account=account).call(item_id=item_id, move_item=move_items)
    mark_as_junk_result = {
        ITEM_ID: item_id,
    }
    if ews_result == "Success":
        mark_as_junk_result[ACTION] = "marked-as-junk"
    else:
        raise Exception("Failed mark-item-as-junk with error: " + ews_result)

    readable_output = tableToMarkdown("Mark item as junk", mark_as_junk_result)
    output = {CONTEXT_UPDATE_EWS_ITEM: mark_as_junk_result}
    return readable_output, output, mark_as_junk_result


def get_items_from_folder(
    client: EWSClient,
    folder_path,
    limit=100,
    target_mailbox=None,
    is_public=None,
    get_internal_item="no",
):
    account = client.get_account(target_mailbox)
    limit = int(limit)
    get_internal_item = get_internal_item == "yes"
    is_public = client.is_default_folder(folder_path, is_public)
    folder = client.get_folder_by_path(folder_path, account, is_public)
    qs = folder.filter().order_by("-datetime_created")[:limit]
    items = get_limited_number_of_messages_from_qs(qs, limit)
    items_result = []

    for item in items:
        item_attachment = parse_item_as_dict(
            item, account.primary_smtp_address, camel_case=True, compact_fields=True
        )
        for attachment in item.attachments:
            if (
                get_internal_item
                and isinstance(attachment, ItemAttachment)
                and isinstance(attachment.item, Message)
            ):
                # if found item attachment - switch item to the attchment
                item_attachment = parse_item_as_dict(
                    attachment.item,
                    account.primary_smtp_address,
                    camel_case=True,
                    compact_fields=True,
                )
                break
        items_result.append(item_attachment)

    hm_headers = [
        "sender",
        "subject",
        "hasAttachments",
        "datetimeReceived",
        "receivedBy",
        "author",
        "toRecipients",
    ]
    # if exchangelib.__version__ == "1.12.0":  # Docker BC
    #     hm_headers.append("itemId") todo: remove if need be
    readable_output = tableToMarkdown(
        "Items in folder " + folder_path, items_result, headers=hm_headers
    )
    output = {CONTEXT_UPDATE_EWS_ITEM: items_result}
    return readable_output, output, items


def get_items(client: EWSClient, item_ids, target_mailbox=None):
    item_ids = argToList(item_ids)
    account = client.get_account(target_mailbox)
    items = client.get_items_from_mailbox(account, item_ids)
    items = [x for x in items if isinstance(x, Message)]
    items_as_incidents = [parse_incident_from_item(client, x) for x in items]
    items_to_context = [
        parse_item_as_dict(x, account.primary_smtp_address, True, True) for x in items
    ]
    readable_output = tableToMarkdown(
        "Get items", items_to_context, ITEMS_RESULTS_HEADERS
    )
    output = {
        CONTEXT_UPDATE_EWS_ITEM: items_to_context,
        "Email": [email_ec(item) for item in items],
    }
    return readable_output, output, items_as_incidents


def get_folder(client: EWSClient, folder_path, target_mailbox=None, is_public=None):
    is_public = client.is_default_folder(folder_path, is_public)
    folder = folder_to_context_entry(
        client.get_folder_by_path(folder_path, is_public=is_public)
    )
    readable_output = tableToMarkdown(f"Folder {folder_path}", folder)
    output = {CONTEXT_UPDATE_FOLDER: folder}
    return readable_output, output, folder


def folder_to_context_entry(f):
    f_entry = {
        "name": f.name,
        "totalCount": f.total_count,
        "id": f.id,
        "childrenFolderCount": f.child_folder_count,
        "changeKey": f.changekey,
    }

    if "unread_count" in [x.name for x in Folder.FIELDS]:
        f_entry["unreadCount"] = f.unread_count
    return f_entry


def mark_item_as_read(
    client: EWSClient, item_ids, operation="read", target_mailbox=None
):
    marked_items = []
    item_ids = argToList(item_ids)
    items = client.get_items_from_mailbox(target_mailbox, item_ids)
    items = [x for x in items if isinstance(x, Message)]

    for item in items:
        item.is_read = operation == "read"
        item.save()

        marked_items.append(
            {
                ITEM_ID: item.item_id,
                MESSAGE_ID: item.message_id,
                ACTION: "marked-as-{}".format(operation),
            }
        )

    readable_output = tableToMarkdown(
        f"Marked items ({operation} marked operation)", marked_items
    )
    output = {CONTEXT_UPDATE_EWS_ITEM: marked_items}
    return readable_output, output, marked_items


def get_item_as_eml(client: EWSClient, item_id, target_mailbox=None):
    account = client.get_account(target_mailbox)
    item = client.get_item_from_mailbox(account, item_id)

    if item.mime_content:
        email_content = email.message_from_string(item.mime_content)
        if item.headers:
            attached_email_headers = [
                (h, " ".join(map(str.strip, v.split("\r\n"))))
                for (h, v) in list(email_content.items())
            ]
            for header in item.headers:
                if (
                    header.name,
                    header.value,
                ) not in attached_email_headers and header.name != "Content-Type":
                    email_content.add_header(header.name, header.value)

        eml_name = item.subject if item.subject else "demisto_untitled_eml"
        file_result = fileResult(eml_name + ".eml", email_content.as_string())
        file_result = (
            file_result if file_result else "Failed uploading eml file to war room"
        )

        return file_result


def test_module(client: EWSClient):
    try:
        account = client.get_account()
        if not account.root.effective_rights.read:  # pylint: disable=E1101
            raise Exception(
                "Success to authenticate, but user has no permissions to read from the mailbox. "
                "Need to delegate the user permissions to the mailbox - "
                "please read integration documentation and follow the instructions"
            )
        client.get_folder_by_path(
            client.folder_name, account, client.is_public_folder
        ).test_access()
    except ErrorFolderNotFound as e:
        if "Top of Information Store" in str(e):
            raise Exception(
                "Success to authenticate, but user probably has no permissions to read from the specific folder."
                "Check user permissions. You can try !ews-find-folders command to "
                "get all the folders structure that the user has permissions to"
            )

    demisto.results("ok")


def sub_main():
    is_test_module = False
    params = demisto.params()
    client = EWSClient(**params)
    args = prepare_args(demisto.args())
    start_logging()
    try:
        command = demisto.command()
        # commands that return a single note result
        normal_commands = {
            "ews-get-searchable-mailboxes": get_searchable_mailboxes,
            "ews-search-mailboxes": search_mailboxes,
            "ews-move-item-between-mailboxes": move_item_between_mailboxes,
            "ews-move-item": move_item,
            "ews-delete-items": delete_items,
            "ews-search-mailbox": search_items_in_mailbox,
            "ews-get-contacts": get_contacts,
            "ews-get-out-of-office": get_out_of_office_state,
            "ews-recover-messages": recover_soft_delete_item,
            "ews-create-folder": create_folder,
            "ews-mark-item-as-junk": mark_item_as_junk,
            "ews-find-folders": find_folders,
            "ews-get-items-from-folder": get_items_from_folder,
            "ews-get-items": get_items,
            "ews-get-folder": get_folder,
            "ews-expand-group": get_expanded_group,
            "ews-mark-items-as-read": mark_item_as_read,
        }

        # commands that may return multiple results or non-note result
        special_output_commands = {
            "ews-get-attachment": fetch_attachments_for_message,
            "ews-delete-attachment": delete_attachments_for_message,
            "ews-get-items-as-eml": get_item_as_eml,
        }
        # system commands:
        if command == "test-module":
            is_test_module = True
            test_module(client)
        elif command == "fetch-incidents":
            incidents = fetch_emails_as_incidents(client)
            demisto.incidents(incidents)

        # special outputs commands
        elif command in special_output_commands:
            demisto.results(*special_output_commands[command](client, **args))  # type: ignore[operator]

        # normal commands
        else:
            return_outputs(*normal_commands[command](client, **args))  # type: ignore[operator]

    except Exception as e:
        time.sleep(2)
        start_logging()
        debug_log = log_stream.getvalue()  # type: ignore
        error_message_simple = ""

        # Office365 regular maintenance case
        if isinstance(e, ErrorMailboxStoreUnavailable) or isinstance(
            e, ErrorMailboxMoveInProgress
        ):
            log_message = (
                "Office365 is undergoing load balancing operations. "
                "As a result, the service is temporarily unavailable."
            )
            if demisto.command() == "fetch-incidents":
                demisto.info(log_message)
                demisto.incidents([])
                sys.exit(0)
            if is_test_module:
                demisto.results(
                    log_message + " Please retry the instance configuration test."
                )
                sys.exit(0)
            error_message_simple = log_message + " Please retry your request."

        if isinstance(e, ConnectionError):
            error_message_simple = (
                "Could not connect to the server.\n"
                "Verify that the Hostname or IP address is correct.\n\n"
                f"Additional information: {str(e)}"
            )
        if isinstance(e, ErrorInvalidPropertyRequest):
            error_message_simple = "Verify that the Exchange version is correct."
        else:
            if is_test_module and isinstance(e, MalformedResponseError):
                error_message_simple = (
                    "Got invalid response from the server.\n"
                    "Verify that the Hostname or IP address is is correct."
                )

        # Legacy error handling
        if "Status code: 401" in debug_log:
            error_message_simple = (
                "Got unauthorized from the server. "
                "Check credentials are correct and authentication method are supported. "
            )

        if "Status code: 503" in debug_log:
            error_message_simple = (
                "Got timeout from the server. "
                "Probably the server is not reachable with the current settings. "
                "Check proxy parameter. If you are using server URL - change to server IP address. "
            )

        if not error_message_simple:
            error_message = error_message_simple = str(e)
        else:
            error_message = error_message_simple + "\n" + str(e)

        stacktrace = traceback.format_exc()
        if stacktrace:
            error_message += "\nFull stacktrace:\n" + stacktrace

        if debug_log:
            error_message += "\nFull debug log:\n" + debug_log

        if demisto.command() == "fetch-incidents":
            raise
        if demisto.command() == "ews-search-mailbox" and isinstance(e, ValueError):
            return_error(
                message="Selected invalid field, please specify valid field name.",
                error=e,
            )
        if is_test_module:
            demisto.results(error_message_simple)
        else:
            demisto.results(
                {
                    "Type": entryTypes["error"],
                    "ContentsFormat": formats["text"],
                    "Contents": error_message_simple,
                }
            )
        demisto.error(f"{e.__class__.__name__}: {error_message}")
    finally:
        exchangelib_cleanup()
        if log_stream:
            try:
                logging.getLogger().removeHandler(log_handler)  # type: ignore
                log_stream.close()
            except Exception as ex:
                demisto.error(
                    "EWS: unexpected exception when trying to remove log handler: {}".format(
                        ex
                    )
                )


def process_main():
    """setup stdin to fd=0 so we can read from the server"""
    sys.stdin = os.fdopen(0, "r")
    sub_main()


def main():
    # When running big queries, like 'ews-search-mailbox' the memory might not freed by the garbage
    # collector. `separate_process` flag will run the integration on a separate process that will prevent
    # memory leakage.
    separate_process = demisto.params().get("separate_process", False)
    demisto.debug("Running as separate_process: {}".format(separate_process))
    if separate_process:
        try:
            p = Process(target=process_main)
            p.start()
            p.join()
        except Exception as ex:
            demisto.error("Failed starting Process: {}".format(ex))
    else:
        sub_main()


from MicrosoftApiModule import *  # noqa: E402


if __name__ in ("__main__", "__builtin__", "builtins"):
    main()
