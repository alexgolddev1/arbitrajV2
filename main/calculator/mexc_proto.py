"""Minimal decoder for MEXC's documented public book-ticker protobuf message."""

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

_file = descriptor_pb2.FileDescriptorProto()
_file.name = "mexc_public_book_ticker.proto"
_file.syntax = "proto3"

book = _file.message_type.add()
book.name = "PublicAggreBookTickerV3Api"
for name, number in (
    ("bidPrice", 1), ("bidQuantity", 2), ("askPrice", 3),
    ("askQuantity", 4), ("version", 5),
):
    field = book.field.add()
    field.name = name
    field.number = number
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING

wrapper = _file.message_type.add()
wrapper.name = "PushDataV3ApiWrapper"
body = wrapper.oneof_decl.add()
body.name = "body"
for name, number, field_type, type_name in (
    ("channel", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING, ""),
    ("symbol", 3, descriptor_pb2.FieldDescriptorProto.TYPE_STRING, ""),
    ("sendTime", 6, descriptor_pb2.FieldDescriptorProto.TYPE_INT64, ""),
    ("publicAggreBookTicker", 315, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
     ".PublicAggreBookTickerV3Api"),
):
    field = wrapper.field.add()
    field.name = name
    field.number = number
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = field_type
    if type_name:
        field.type_name = type_name
        field.oneof_index = 0

_pool = descriptor_pool.DescriptorPool()
_pool.Add(_file)
PushDataV3ApiWrapper = message_factory.GetMessageClass(
    _pool.FindMessageTypeByName("PushDataV3ApiWrapper")
)
