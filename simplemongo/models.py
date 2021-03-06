#!/usr/bin/env python
# -*- coding: utf-8 -*-

# simple orm wrapper of MongoDB using pymongo

import copy
import logging
from bson.objectid import ObjectId
from pymongo.collection import Collection
from . import errors
from .dstruct import StructuredDict, StructuredDictMetaclass, diff_dicts
from .cursor import SimplemongoCursor, Cursor


# TODO replace logging to certain logger


def oid(id):
    if isinstance(id, ObjectId):
        return id
    elif isinstance(id, (str, unicode)):
        if isinstance(id, unicode):
            id = id.encode('utf8')
        return ObjectId(id)
    else:
        raise ValueError('get type %s, should be str/unicode or ObjectId' % type(id))


class DocumentMetaclass(StructuredDictMetaclass):
    """
    use for judging if Document's subclasses have assign attribute 'col' properly
    """
    def __new__(cls, name, bases, attrs):

        # # Repeat code in dstruct.StructuredDictMetaclass.__new__
        # if 'struct' in attrs:
        #     check_struct(attrs['struct'])

        # test if the target class is Document
        if not (len(bases) == 1 and bases[0] is StructuredDict):

            # check collection
            if 'col' not in attrs:
                raise errors.StructError('`col` attribute should be assigned for Document subclass')
            if not isinstance(attrs['col'], Collection):
                raise errors.StructError(
                    '`col` should be pymongo.Collection instance, received: %s %s' %
                    (attrs['col'], type(attrs['col'])))

        # return type.__new__(cls, name, bases, attrs)
        return StructuredDictMetaclass.__new__(cls, name, bases, attrs)


class Document(StructuredDict):
    """A wrapper of MongoDB Document, can also be used to init new document.

    Acturally, a Document is a representation of one certaion collectino which store
    data in structure of the Document, they two are of one-to-one relation

    By default all the fields in struct are not required to **exist**
    if exist, None value is allowed
    there are two lists to mark field option
    1. required_fields
       a field in required fields must exist in doc, despite it value type
    2. strict_fields
       a field in strict_fields must be exactly the type defined in struct,
       that means, could not be None (the only exception is defined type is None)
    So there are 4 situations for a field:
    1. not required and not strict
       it can be:
       - not exist
       - exist and value is instance of type
       - exist and value is None
    2. required and not strict
       it can be:
       - exist and value is instance of type
       - exist and value is None
    3. not required and strict
       it can be:
       - not exist
       - exist and value is instance of type
    4. required and strict
       it can only be:
       - exist and value is instance of type
    Additionally, a field that is not defined in struct will not be handled,
    no matter what value it is. a list to restrict fields that can't be exist
    is not considered to be implemented currently.

    Usage:
    1. create new document
    >>> class ADoc(Document):
    ...     col = db['dbtest']['coltest']
    ...

    2. init from existing document

    """
    __metaclass__ = DocumentMetaclass

    __safe_operation__ = True

    __write_concern__ = {
        'w': 1,
        'j': False
    }

    # validate only works on `save` method
    __validate__ = True

    def __init__(self, raw=None, from_db=False):
        """ wrapper of raw data from cursor

        NOTE *initialize without validation*
        """
        self._in_db = from_db

        if raw is None:
            assert self._in_db is False
            super(Document, self).__init__()
            self._raw = None
        else:
            if self._in_db:
                # Use deepcopy to isolate raw and Document itself
                super(Document, self).__init__(copy.deepcopy(raw))
                self._raw = raw
            else:
                super(Document, self).__init__(raw)
                self._raw = None

        # A document instance can be get in 3 ways:
        # 1. Document(raw)
        #    No _id unless passed in parameters or save
        #    No self._raw until/unless save
        # 2. Document.new(**raw)
        #    Has _id auto created
        #    No self._raw until/unless save
        # 3. Document.find() <=> Document(raw, True)
        #    Has _id
        #    Has self._raw

    def __str__(self):
        return '<Document: %s >' % dict(self)

    def deepcopy(self):
        return copy.deepcopy(self)

    @property
    def identifier(self):
        return {'_id': self['_id']}

    def _get_write_options(self, **kwgs):
        options = self.__class__.__write_concern__.copy()
        options.update(kwgs)
        return options

    def save(self):
        if self.__class__.__validate__:
            logging.debug('__validate__ is on')
            self.validate()

        if '_id' not in self:
            self['_id'] = ObjectId()
            logging.debug('_id generated %s' % self['_id'])

        if self._raw is None:
            self._raw = copy.deepcopy(dict(self))

        rv = self.col.save(self, **self._get_write_options(manipulate=True))
        logging.debug('ObjectId(%s) saved' % rv)
        self._in_db = True
        return rv

    def remove(self):
        assert self._in_db, 'Could not remove document which is not in database'
        self._history = self.copy()
        _id = self['_id']
        self.col.remove(_id, **self._get_write_options())
        logging.debug('%s removed' % _id)
        self.clear()
        self._in_db = False

    def update_self(self, spec, **kwargs):
        options = self._get_write_options(**kwargs)
        # Make sure `multi` is False
        options['multi'] = False
        rv = self.col.update(
            self.identifier, spec, **options)
        return rv

    @property
    def changes(self):
        if not self._raw:
            return None
        c = {}

        diff = diff_dicts(self, self._raw)

        # $set & $inc
        if diff['+']:
            c['$set'] = diff['+']

        # $inc
        for i in diff['~']:
            if isinstance(self[i], int) and isinstance(self._raw[i], int):
                inc = c.setdefault('$inc', {})
                inc[i] = self[i] - self._raw[i]
            else:
                set_ = c.setdefault('$set', {})
                set_[i] = self[i]

        # $unset
        if diff['-']:
            c['$unset'] = diff['-']

        return c

    def update_changes(self, **kwargs):
        c = self.changes
        if c:
            logging.debug('update changes: %s', c)
            self.update_self(c, **kwargs)
        else:
            logging.debug('no changes to update')

    def pull(self):
        """Update document from database
        """
        cursor = Cursor(self.col, self.identifier)
        try:
            doc = cursor.next()
        except StopIteration:
            raise errors.SimplemongoException('Document was deleted before `pull` was called')
        self.clear()
        self.update(doc)

    @classmethod
    def insert(cls, *args, **kwargs):
        pass

    @classmethod
    def new(cls, **kwargs):
        """Create a new model instance, with _id generated,
        initialize by structure of self.struct
        """
        if '_id' not in kwargs:
            kwargs['_id'] = ObjectId()
            logging.debug('_id generated %s' % kwargs['_id'])
        instance = cls.build_instance(**kwargs)
        return instance

    @classmethod
    def find(cls, *args, **kwargs):
        logging.debug('find: %s, %s', args, kwargs)
        kwargs['wrapper'] = cls
        cursor = SimplemongoCursor(cls.col, *args, **kwargs)
        return cursor

    @classmethod
    def one(cls, spec_or_id, allow_multiple=False, *args, **kwargs):
        if spec_or_id is not None and not isinstance(spec_or_id, dict):
            spec_or_id = {"_id": spec_or_id}

        cursor = cls.find(spec_or_id, *args, **kwargs)
        if not allow_multiple:
            count = cursor.count()
            if count > 1:
                raise errors.MultipleObjectsReturned(
                    'explain: %s' % cursor.explain())
        for doc in cursor:
            return doc
        return None

    @classmethod
    def one_or_raise(cls, *args, **kwargs):
        rv = cls.one(*args, **kwargs)
        if rv is None:
            raise errors.ObjectNotFound('Could not find: %s, %s' % (args, kwargs))
        return rv
