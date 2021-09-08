# Released under the MIT License. See LICENSE for details.
#
"""Functionality for sending and responding to messages.
Supports static typing for message types and possible return types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar
from dataclasses import dataclass
from enum import Enum
import inspect
import logging
import json
import traceback

from typing_extensions import Annotated

from efro.error import CleanError, RemoteError
from efro.dataclassio import (ioprepped, is_ioprepped_dataclass, IOAttrs,
                              dataclass_to_dict, dataclass_from_dict)

if TYPE_CHECKING:
    from typing import (Dict, Type, Tuple, List, Any, Callable, Optional, Set,
                        Sequence, Union)
    from efro.error import CommunicationError

TM = TypeVar('TM', bound='MessageSender')


class RemoteErrorType(Enum):
    """Type of error that occurred in remote message handling."""
    OTHER = 0
    CLEAN = 1


class Message:
    """Base class for messages."""

    @classmethod
    def get_response_types(cls) -> List[Type[Response]]:
        """Return all message types this Message can result in when sent.
        Note: RemoteErrorMessage is handled transparently and does not
        need to be specified here.
        """
        return []


class Response:
    """Base class for responses to messages."""


# Some standard response types:


@ioprepped
@dataclass
class RemoteErrorResponse(Response):
    """Message saying some error has occurred on the other end."""
    error_message: Annotated[str, IOAttrs('m')]
    error_type: Annotated[RemoteErrorType, IOAttrs('t')]


@ioprepped
@dataclass
class EmptyResponse(Response):
    """The response equivalent of None."""


class MessageProtocol:
    """Wrangles a set of message types, formats, and response types.
    Both endpoints must be using a compatible Protocol for communication
    to succeed. To maintain Protocol compatibility between revisions,
    all message types must retain the same id, message attr storage names must
    not change, newly added attrs must have default values, etc.
    """

    def __init__(self,
                 message_types: Dict[int, Type[Message]],
                 response_types: Dict[int, Type[Response]],
                 type_key: Optional[str] = None,
                 preserve_clean_errors: bool = True,
                 log_remote_exceptions: bool = True,
                 trusted_client: bool = False) -> None:
        """Create a protocol with a given configuration.

        If 'type_key' is provided, the message type ID is stored as the
        provided key in the message dict; otherwise it will be stored as
        part of a top level dict with the message payload appearing as a
        child dict. This is mainly for backwards compatibility.

        If 'preserve_clean_errors' is True, efro.error.CleanError errors
        on the remote end will result in the same error raised locally.
        All other Exception types come across as efro.error.RemoteError.

        If 'trusted_client' is True, stringified remote stack traces will
        be included in the RemoteError. This should only be enabled in cases
        where the client is trusted.
        """
        self.message_types_by_id: Dict[int, Type[Message]] = {}
        self.message_ids_by_type: Dict[Type[Message], int] = {}
        self.response_types_by_id: Dict[int, Type[Response]] = {}
        self.response_ids_by_type: Dict[Type[Response], int] = {}
        for m_id, m_type in message_types.items():

            # Make sure only valid message types were passed and each
            # id was assigned only once.
            assert isinstance(m_id, int)
            assert m_id >= 0
            assert (is_ioprepped_dataclass(m_type)
                    and issubclass(m_type, Message))
            assert self.message_types_by_id.get(m_id) is None
            self.message_types_by_id[m_id] = m_type
            self.message_ids_by_type[m_type] = m_id

        for r_id, r_type in response_types.items():
            assert isinstance(r_id, int)
            assert r_id >= 0
            assert (is_ioprepped_dataclass(r_type)
                    and issubclass(r_type, Response))
            assert self.response_types_by_id.get(r_id) is None
            self.response_types_by_id[r_id] = r_type
            self.response_ids_by_type[r_type] = r_id

        # Some extra-thorough validation in debug mode.
        if __debug__:
            # Make sure all Message types' return types are valid
            # and have been assigned an ID as well.
            all_response_types: Set[Type[Response]] = set()
            for m_id, m_type in message_types.items():
                m_rtypes = m_type.get_response_types()
                assert isinstance(m_rtypes, list)
                assert m_rtypes, (
                    f'Message type {m_type} specifies no return types.')
                assert len(set(m_rtypes)) == len(m_rtypes)  # check dups
                all_response_types.update(m_rtypes)
            for cls in all_response_types:
                assert is_ioprepped_dataclass(cls) and issubclass(
                    cls, Response)
                if cls not in self.response_ids_by_type:
                    raise ValueError(f'Possible response type {cls}'
                                     f' was not included in response_types.')

            # Make sure all registered types have unique base names.
            # We can take advantage of this to generate cleaner looking
            # protocol modules. Can revisit if this is ever a problem.
            mtypenames = set(tp.__name__ for tp in self.message_ids_by_type)
            if len(mtypenames) != len(message_types):
                raise ValueError(
                    'message_types contains duplicate __name__s;'
                    ' all types are required to have unique names.')

        self._type_key = type_key
        self.preserve_clean_errors = preserve_clean_errors
        self.log_remote_exceptions = log_remote_exceptions
        self.trusted_client = trusted_client

    def encode_message(self,
                       message: Message,
                       is_error: bool = False) -> bytes:
        """Encode a message to bytes for transport."""
        return self._encode(message, is_error, self.message_ids_by_type,
                            'message')

    def encode_response(self,
                        response: Response,
                        is_error: bool = False) -> bytes:
        """Encode a response to bytes for transport."""
        return self._encode(response, is_error, self.response_ids_by_type,
                            'response')

    def _encode(self, message: Any, is_error: bool,
                ids_by_type: Dict[Type, int], opname: str) -> bytes:
        """Encode a message to bytes for transport."""

        m_id: Optional[int]
        if is_error:
            m_id = -1
        else:
            m_id = ids_by_type.get(type(message))
            if m_id is None:
                raise TypeError(f'{opname} type is not registered in protocol:'
                                f' {type(message)}')
        msgdict = dataclass_to_dict(message)

        # Encode type as part of the message/response dict if desired
        # (for legacy compatibility).
        if self._type_key is not None:
            if self._type_key in msgdict:
                raise RuntimeError(f'Type-key {self._type_key}'
                                   f' found in msg of type {type(message)}')
            msgdict[self._type_key] = m_id
            out = msgdict
        else:
            out = {'m': msgdict, 't': m_id}
        return json.dumps(out, separators=(',', ':')).encode()

    def decode_message(self, data: bytes) -> Message:
        """Decode a message from bytes."""
        out = self._decode(data, self.message_types_by_id, 'message')
        assert isinstance(out, Message)
        return out

    def decode_response(self, data: bytes) -> Response:
        """Decode a response from bytes."""
        out = self._decode(data, self.response_types_by_id, 'response')
        assert isinstance(out, Response)
        return out

    def _decode(self, data: bytes, types_by_id: Dict[int, Type],
                opname: str) -> Any:
        """Decode a message from bytes."""
        msgfull = json.loads(data.decode())
        assert isinstance(msgfull, dict)
        msgdict: Optional[dict]
        if self._type_key is not None:
            m_id = msgfull.pop(self._type_key)
            msgdict = msgfull
            assert isinstance(m_id, int)
        else:
            m_id = msgfull.get('t')
            msgdict = msgfull.get('m')
        assert isinstance(m_id, int)
        assert isinstance(msgdict, dict)

        # Special case: a remote error occurred. Raise a local Exception.
        if m_id == -1:
            assert opname == 'response'
            err = dataclass_from_dict(RemoteErrorResponse, msgdict)
            if (self.preserve_clean_errors
                    and err.error_type is RemoteErrorType.CLEAN):
                raise CleanError(err.error_message)
            raise RemoteError(err.error_message)

        # Decode this particular type.
        msgtype = types_by_id.get(m_id)
        if msgtype is None:
            raise TypeError(f'Got unregistered {opname} type id of {m_id}.')
        return dataclass_from_dict(msgtype, msgdict)

    def _get_module_header(self, part: str) -> str:
        """Return common parts of generated modules."""
        imports: Dict[str, List[str]] = {}
        for msgtype in self.message_ids_by_type:
            imports.setdefault(msgtype.__module__, []).append(msgtype.__name__)
        importlines = ''
        for module, names in sorted(imports.items()):
            jnames = ', '.join(names)
            line = f'from {module} import {jnames}'
            if len(line) > 79:
                # Recreate in a wrapping-friendly form.
                line = f'from {module} import ({jnames})'
            importlines += f'{line}\n'

        if part == 'sender':
            importlines = (
                f'from efro.message import MessageSender\n{importlines}')
            tpimports = 'from efro.message import Message, Response'
        else:
            importlines = (
                f'from efro.message import MessageReceiver\n{importlines}')
            tpimports = 'from efro.message import Message, Response'

        out = ('# Released under the MIT License. See LICENSE for details.\n'
               f'#\n'
               f'"""Auto-generated {part} module."""\n'
               f'\n'
               f'from __future__ import annotations\n'
               f'\n'
               f'from typing import TYPE_CHECKING, overload\n'
               f'\n'
               f'{importlines}'
               f'\n'
               f'if TYPE_CHECKING:\n'
               f'    from typing import Union\n'
               f'    {tpimports}\n'
               f'\n'
               f'\n')
        return out

    def create_sender_module(self,
                             classname: str,
                             private: bool = False) -> str:
        """"Create a Python module defining a MessageSender subclass.

        This class is primarily for type checking and will contain overrides
        for the varieties of send calls for message/response types defined
        in the protocol.

        Note that line lengths are not clipped, so output may need to be
        run through a formatter to prevent lint warnings about excessive
        line lengths.
        """

        ppre = '_' if private else ''
        out = self._get_module_header('sender')
        out += (f'class {ppre}{classname}MessageSender(MessageSender):\n'
                f'    """Protocol-specific sender."""\n'
                f'\n'
                f'    def __get__(self,\n'
                f'                obj: Any,\n'
                f'                type_in: Any = None)'
                f' -> {ppre}Bound{classname}MessageSender:\n'
                f'        return {ppre}Bound{classname}MessageSender'
                f'(obj, self)\n'
                f'\n'
                f'\n'
                f'class {ppre}Bound{classname}MessageSender:\n'
                f'    """Protocol-specific bound sender."""\n'
                f'\n'
                f'    def __init__(self, obj: Any,'
                f' sender: {ppre}{classname}MessageSender) -> None:\n'
                f'        assert obj is not None\n'
                f'        self._obj = obj\n'
                f'        self._sender = sender\n')

        # Define handler() overloads for all registered message types.
        msgtypes = [
            t for t in self.message_ids_by_type if issubclass(t, Message)
        ]

        # Ew; @overload requires at least 2 different signatures so
        # we need to simply write a single function if we have < 2.
        if len(msgtypes) == 1:
            raise RuntimeError('FIXME: currently we require at least 2'
                               ' registered message types; found 1.')
        if len(msgtypes) > 1:
            for msgtype in msgtypes:
                msgtypevar = msgtype.__name__
                rtypes = msgtype.get_response_types()
                if len(rtypes) > 1:
                    tps = ', '.join(t.__name__ for t in rtypes)
                    responsetypevar = f'Union[{tps}]'
                else:
                    responsetypevar = rtypes[0].__name__
                out += (f'\n'
                        f'    @overload\n'
                        f'    def send(self, message: {msgtypevar})'
                        f' -> {responsetypevar}:\n'
                        f'        ...\n')
            out += ('\n'
                    '    def send(self, message: Message) -> Response:\n'
                    '        """Send a message."""\n'
                    '        return self._sender.send(self._obj, message)\n')

        return out

    def create_receiver_module(self,
                               classname: str,
                               private: bool = False) -> str:
        """"Create a Python module defining a MessageReceiver subclass.

        This class is primarily for type checking and will contain overrides
        for the register method for message/response types defined in
        the protocol.

        Note that line lengths are not clipped, so output may need to be
        run through a formatter to prevent lint warnings about excessive
        line lengths.
        """
        ppre = '_' if private else ''
        out = self._get_module_header('receiver')
        out += (f'class {ppre}{classname}MessageReceiver(MessageReceiver):\n'
                f'    """Protocol-specific receiver."""\n'
                f'\n'
                f'    def __get__(\n'
                f'        self,\n'
                f'        obj: Any,\n'
                f'        type_in: Any = None,\n'
                f'    ) -> {ppre}Bound{classname}MessageReceiver:\n'
                f'        return {ppre}Bound{classname}MessageReceiver('
                f'obj, self)\n')

        # Define handler() overloads for all registered message types.
        msgtypes = [
            t for t in self.message_ids_by_type if issubclass(t, Message)
        ]

        # Ew; @overload requires at least 2 different signatures so
        # we need to simply write a single function if we have < 2.
        if len(msgtypes) == 1:
            raise RuntimeError('FIXME: currently require at least 2'
                               ' registered message types; found 1.')
        if len(msgtypes) > 1:
            for msgtype in msgtypes:
                msgtypevar = msgtype.__name__
                rtypes = msgtype.get_response_types()
                if len(rtypes) > 1:
                    tps = ', '.join(t.__name__ for t in rtypes)
                    rtypevar = f'Union[{tps}]'
                else:
                    rtypevar = rtypes[0].__name__
                out += (
                    f'\n'
                    f'    @overload\n'
                    f'    def handler(\n'
                    f'        self,\n'
                    f'        call: Callable[[Any, {msgtypevar}], '
                    f'{rtypevar}],\n'
                    f'    ) -> Callable[[Any, {msgtypevar}], {rtypevar}]:\n'
                    f'        ...\n')
            out += ('\n'
                    '    def handler(self, call: Callable) -> Callable:\n'
                    '        """Decorator to register message handlers."""\n'
                    '        self.register_handler(call)\n'
                    '        return call\n')

        out += (f'\n'
                f'\n'
                f'class {ppre}Bound{classname}MessageReceiver:\n'
                f'    """Protocol-specific bound receiver."""\n'
                f'\n'
                f'    def __init__(\n'
                f'        self,\n'
                f'        obj: Any,\n'
                f'        receiver: _TestMessageReceiver,\n'
                f'    ) -> None:\n'
                f'        assert obj is not None\n'
                f'        self._obj = obj\n'
                f'        self._receiver = receiver\n'
                f'\n'
                f'    def handle_raw_message(self, message: bytes) -> bytes:\n'
                f'        """Handle a raw incoming message."""\n'
                f'        return self._receiver.handle_raw_message'
                f'(self._obj, message)\n')

        return out


class MessageSender:
    """Facilitates sending messages to a target and receiving responses.
    This is instantiated at the class level and used to register unbound
    class methods to handle raw message sending.

    Example:

    class MyClass:
        msg = MyMessageSender(some_protocol)

        @msg.send_raw_handler
        def send_raw_message(self, message: bytes) -> bytes:
            # Actually send the message here.

    # MyMessageSender class should provide overloads for send(), send_bg(),
    # etc. to ensure all sending happens with valid types.
    obj = MyClass()
    obj.msg.send(SomeMessageType())
    """

    def __init__(self, protocol: MessageProtocol) -> None:
        self._protocol = protocol
        self._send_raw_message_call: Optional[Callable[[Any, bytes],
                                                       bytes]] = None

    def send_raw_handler(
            self, call: Callable[[Any, bytes],
                                 bytes]) -> Callable[[Any, bytes], bytes]:
        """Function decorator for setting raw send method."""
        assert self._send_raw_message_call is None
        self._send_raw_message_call = call
        return call

    def send(self, bound_obj: Any, message: Message) -> Response:
        """Send a message and receive a response.

        Will encode the message for transport and call dispatch_raw_message()
        """
        if self._send_raw_message_call is None:
            raise RuntimeError('send() is unimplemented for this type.')

        msg_encoded = self._protocol.encode_message(message)
        response_encoded = self._send_raw_message_call(bound_obj, msg_encoded)
        response = self._protocol.decode_response(response_encoded)
        assert isinstance(response, Response)
        assert type(response) in type(message).get_response_types()
        return response

    def send_bg(self, bound_obj: Any, message: Message) -> Message:
        """Send a message asynchronously and receive a future.

        The message will be encoded for transport and passed to
        dispatch_raw_message from a background thread.
        """
        raise RuntimeError('Unimplemented!')

    def send_async(self, bound_obj: Any, message: Message) -> Message:
        """Send a message asynchronously using asyncio.

        The message will be encoded for transport and passed to
        dispatch_raw_message_async.
        """
        raise RuntimeError('Unimplemented!')


class MessageReceiver:
    """Facilitates receiving & responding to messages from a remote source.

    This is instantiated at the class level with unbound methods registered
    as handlers for different message types in the protocol.

    Example:

    class MyClass:
        receiver = MyMessageReceiver()

        # MyMessageReceiver fills out handler() overloads to ensure all
        # registered handlers have valid types/return-types.
        @receiver.handler
        def handle_some_message_type(self, message: SomeMsg) -> SomeResponse:
            # Deal with this message type here.

    # This will trigger the registered handler being called.
    obj = MyClass()
    obj.receiver.handle_raw_message(some_raw_data)

    Any unhandled Exception occurring during message handling will result in
    an Exception being raised on the sending end.
    """

    def __init__(self, protocol: MessageProtocol) -> None:
        self._protocol = protocol
        self._handlers: Dict[Type[Message], Callable] = {}

    # noinspection PyProtectedMember
    def register_handler(self, call: Callable[[Any, Message],
                                              Response]) -> None:
        """Register a handler call.

        The message type handled by the call is determined by its
        type annotation.
        """
        # TODO: can use types.GenericAlias in 3.9.
        from typing import _GenericAlias  # type: ignore
        from typing import Union, get_type_hints, get_args

        sig = inspect.getfullargspec(call)

        # The provided callable should be a method taking one 'msg' arg.
        expectedsig = ['self', 'msg']
        if sig.args != expectedsig:
            raise ValueError(f'Expected callable signature of {expectedsig};'
                             f' got {sig.args}')

        # Check annotation types to determine what message types we handle.
        # Return-type annotation can be a Union, but we probably don't
        # have it available at runtime. Explicitly pull it in.
        anns = get_type_hints(call, localns={'Union': Union})
        msgtype = anns.get('msg')
        if not isinstance(msgtype, type):
            raise TypeError(
                f'expected a type for "msg" annotation; got {type(msgtype)}.')
        assert issubclass(msgtype, Message)

        ret = anns.get('return')
        responsetypes: Tuple[Type, ...]

        # Return types can be a single type or a union of types.
        if isinstance(ret, _GenericAlias):
            targs = get_args(ret)
            if not all(isinstance(a, type) for a in targs):
                raise TypeError(f'expected only types for "return" annotation;'
                                f' got {targs}.')
            responsetypes = targs
        else:
            if not isinstance(ret, type):
                raise TypeError(f'expected one or more types for'
                                f' "return" annotation; got a {type(ret)}.')
            responsetypes = (ret, )

        # Make sure our protocol has this message type registered and our
        # return types exactly match. (Technically we could return a subset
        # of the supported types; can allow this in the future if it makes
        # sense).
        registered_types = self._protocol.message_ids_by_type.keys()

        if msgtype not in registered_types:
            raise TypeError(f'Message type {msgtype} is not registered'
                            f' in this Protocol.')

        if msgtype in self._handlers:
            raise TypeError(f'Message type {msgtype} already has a registered'
                            f' handler.')

        # Make sure the responses exactly matches what the message expects.
        if set(responsetypes) != set(msgtype.get_response_types()):
            raise TypeError(
                f'Provided response types {responsetypes} do not'
                f' match the set expected by message type {msgtype}: '
                f'({msgtype.get_response_types()})')

        # Ok; we're good!
        self._handlers[msgtype] = call

    def validate(self, warn_only: bool = False) -> None:
        """Check for handler completeness, valid types, etc."""
        for msgtype in self._protocol.message_ids_by_type.keys():
            if issubclass(msgtype, Response):
                continue
            if msgtype not in self._handlers:
                msg = (f'Protocol message {msgtype} not handled'
                       f' by receiver.')
                if warn_only:
                    logging.warning(msg)
                raise TypeError(msg)

    def handle_raw_message(self, bound_obj: Any, msg: bytes) -> bytes:
        """Decode, handle, and return encoded response for a message."""
        try:
            # Decode the incoming message.
            msg_decoded = self._protocol.decode_message(msg)
            msgtype = type(msg_decoded)
            assert issubclass(msgtype, Message)

            # Call the proper handler.
            handler = self._handlers.get(msgtype)
            if handler is None:
                raise RuntimeError(f'Got unhandled message type: {msgtype}.')
            response = handler(bound_obj, msg_decoded)

            # Re-encode the response.
            assert isinstance(response, Response)
            assert type(response) in msgtype.get_response_types()
            return self._protocol.encode_response(response)

        except Exception as exc:

            if self._protocol.log_remote_exceptions:
                logging.exception('Error handling message.')

            # If anything goes wrong, return a RemoteErrorResponse instead.
            if (isinstance(exc, CleanError)
                    and self._protocol.preserve_clean_errors):
                err_response = RemoteErrorResponse(
                    error_message=str(exc), error_type=RemoteErrorType.CLEAN)

            else:

                err_response = RemoteErrorResponse(
                    error_message=(traceback.format_exc()
                                   if self._protocol.trusted_client else
                                   'An unknown error has occurred.'),
                    error_type=RemoteErrorType.OTHER)
            return self._protocol.encode_response(err_response, is_error=True)

    async def handle_raw_message_async(self, msg: bytes) -> bytes:
        """Should be called when the receiver gets a message.

        The return value is the raw response to the message.
        """
        raise RuntimeError('Unimplemented!')
