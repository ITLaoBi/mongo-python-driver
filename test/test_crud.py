# Copyright 2015 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test the collection module."""

import json
import os
import re
import sys

sys.path[0:0] = [""]

from bson.py3compat import iteritems
from pymongo import operations
from pymongo.command_cursor import CommandCursor
from pymongo.cursor import Cursor
from pymongo.results import _WriteResult, BulkWriteResult
from pymongo.operations import (InsertOne,
                                DeleteOne,
                                DeleteMany,
                                ReplaceOne,
                                UpdateOne,
                                UpdateMany)

from test import unittest, client_context, IntegrationTest

# Location of JSON test specifications.
_TEST_PATH = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), 'crud')


class TestAllScenarios(IntegrationTest):
    pass


def camel_to_snake(camel):
    # Regex to convert CamelCase to snake_case.
    snake = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', camel)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', snake).lower()


def camel_to_upper_camel(camel):
    return camel[0].upper() + camel[1:]


def check_result(expected_result, result):
    if isinstance(result, Cursor) or isinstance(result, CommandCursor):
        return list(result) == expected_result

    elif isinstance(result, _WriteResult):
        for res in expected_result:
            prop = camel_to_snake(res)
            # SPEC-869: Only BulkWriteResult has upserted_count.
            if (prop == "upserted_count" and
                    not isinstance(result, BulkWriteResult)):
                if result.upserted_id is not None:
                    upserted_count = 1
                else:
                    upserted_count = 0
                if upserted_count != expected_result[res]:
                    return False
            elif prop == "inserted_ids":
                # BulkWriteResult does not have inserted_ids.
                if isinstance(result, BulkWriteResult):
                    if len(expected_result[res]) != result.inserted_count:
                        return False
                else:
                    # InsertManyResult may be compared to [id1] from the
                    # crud spec or {"0": id1} from the retryable write spec.
                    ids = expected_result[res]
                    if isinstance(ids, dict):
                        ids = [ids[str(i)] for i in range(len(ids))]
                    if ids != result.inserted_ids:
                        return False
            elif prop == "upserted_ids":
                # Convert indexes from strings to integers.
                ids = expected_result[res]
                expected_ids = {}
                for str_index in ids:
                    expected_ids[int(str_index)] = ids[str_index]
                if expected_ids != result.upserted_ids:
                    return False
            elif getattr(result, prop) != expected_result[res]:
                return False
        return True
    else:
        if not expected_result:
            return result is None
        else:
            return result == expected_result


def camel_to_snake_args(arguments):
    for arg_name in list(arguments):
        c2s = camel_to_snake(arg_name)
        arguments[c2s] = arguments.pop(arg_name)
    return arguments


def run_operation(collection, test):
    # Convert command from CamelCase to pymongo.collection method.
    operation = camel_to_snake(test['operation']['name'])
    cmd = getattr(collection, operation)

    # Convert arguments to snake_case and handle special cases.
    arguments = test['operation']['arguments']
    options = arguments.pop("options", {})
    for option_name in options:
        arguments[camel_to_snake(option_name)] = options[option_name]
    if operation == "bulk_write":
        # Parse each request into a bulk write model.
        requests = []
        for request in arguments["requests"]:
            bulk_model = camel_to_upper_camel(request["name"])
            bulk_class = getattr(operations, bulk_model)
            bulk_arguments = camel_to_snake_args(request["arguments"])
            requests.append(bulk_class(**bulk_arguments))
        arguments["requests"] = requests
    else:
        for arg_name in list(arguments):
            c2s = camel_to_snake(arg_name)
            # PyMongo accepts sort as list of tuples. Asserting len=1
            # because ordering dicts from JSON in 2.6 is unwieldy.
            if arg_name == "sort":
                sort_dict = arguments[arg_name]
                assert len(sort_dict) == 1, 'test can only have 1 sort key'
                arguments[arg_name] = list(iteritems(sort_dict))
            # Named "key" instead not fieldName.
            if arg_name == "fieldName":
                arguments["key"] = arguments.pop(arg_name)
            # Aggregate uses "batchSize", while find uses batch_size.
            elif arg_name == "batchSize" and operation == "aggregate":
                continue
            # Requires boolean returnDocument.
            elif arg_name == "returnDocument":
                arguments[c2s] = arguments[arg_name] == "After"
            else:
                arguments[c2s] = arguments.pop(arg_name)

    return cmd(**arguments)


def create_test(scenario_def, test):
    def run_scenario(self):
        # Load data.
        assert scenario_def['data'], "tests must have non-empty data"
        self.db.test.drop()
        self.db.test.insert_many(scenario_def['data'])

        result = run_operation(self.db.test, test)

        # Assert final state is expected.
        expected_c = test['outcome'].get('collection')
        if expected_c is not None:
            expected_name = expected_c.get('name')
            if expected_name is not None:
                db_coll = self.db[expected_name]
            else:
                db_coll = self.db.test
            self.assertEqual(list(db_coll.find()), expected_c['data'])
        expected_result = test['outcome'].get('result')
        # aggregate $out cursors return no documents.
        if test['description'] == 'Aggregate with $out':
            expected_result = []
        self.assertTrue(check_result(expected_result, result))

    return run_scenario


def create_tests():
    for dirpath, _, filenames in os.walk(_TEST_PATH):
        dirname = os.path.split(dirpath)[-1]

        for filename in filenames:
            with open(os.path.join(dirpath, filename)) as scenario_stream:
                scenario_def = json.load(scenario_stream)

            test_type = os.path.splitext(filename)[0]

            min_ver, max_ver = None, None
            if 'minServerVersion' in scenario_def:
                min_ver = tuple(
                    int(elt) for
                    elt in scenario_def['minServerVersion'].split('.'))
            if 'maxServerVersion' in scenario_def:
                max_ver = tuple(
                    int(elt) for
                    elt in scenario_def['maxServerVersion'].split('.'))

            # Construct test from scenario.
            for test in scenario_def['tests']:
                new_test = create_test(scenario_def, test)
                if min_ver is not None:
                    new_test = client_context.require_version_min(*min_ver)(
                        new_test)
                if max_ver is not None:
                    new_test = client_context.require_version_max(*max_ver)(
                        new_test)

                test_name = 'test_%s_%s_%s' % (
                    dirname,
                    test_type,
                    str(test['description'].replace(" ", "_")))

                new_test.__name__ = test_name
                setattr(TestAllScenarios, new_test.__name__, new_test)


create_tests()


class TestWriteOpsComparison(unittest.TestCase):
    def test_InsertOneEquals(self):
        self.assertEqual(InsertOne({'foo': 42}), InsertOne({'foo': 42}))

    def test_InsertOneNotEquals(self):
        self.assertNotEqual(InsertOne({'foo': 42}), InsertOne({'foo': 23}))

    def test_DeleteOneEquals(self):
        self.assertEqual(DeleteOne({'foo': 42}), DeleteOne({'foo': 42}))

    def test_DeleteOneNotEquals(self):
        self.assertNotEqual(DeleteOne({'foo': 42}), DeleteOne({'foo': 23}))

    def test_DeleteManyEquals(self):
        self.assertEqual(DeleteMany({'foo': 42}), DeleteMany({'foo': 42}))

    def test_DeleteManyNotEquals(self):
        self.assertNotEqual(DeleteMany({'foo': 42}), DeleteMany({'foo': 23}))

    def test_DeleteOneNotEqualsDeleteMany(self):
        self.assertNotEqual(DeleteOne({'foo': 42}), DeleteMany({'foo': 42}))

    def test_ReplaceOneEquals(self):
        self.assertEqual(ReplaceOne({'foo': 42}, {'bar': 42}, upsert=False),
                         ReplaceOne({'foo': 42}, {'bar': 42}, upsert=False))

    def test_ReplaceOneNotEquals(self):
        self.assertNotEqual(ReplaceOne({'foo': 42}, {'bar': 42}, upsert=False),
                            ReplaceOne({'foo': 42}, {'bar': 42}, upsert=True))

    def test_UpdateOneEquals(self):
        self.assertEqual(UpdateOne({'foo': 42}, {'$set': {'bar': 42}}),
                         UpdateOne({'foo': 42}, {'$set': {'bar': 42}}))

    def test_UpdateOneNotEquals(self):
        self.assertNotEqual(UpdateOne({'foo': 42}, {'$set': {'bar': 42}}),
                            UpdateOne({'foo': 42}, {'$set': {'bar': 23}}))

    def test_UpdateManyEquals(self):
        self.assertEqual(UpdateMany({'foo': 42}, {'$set': {'bar': 42}}),
                         UpdateMany({'foo': 42}, {'$set': {'bar': 42}}))

    def test_UpdateManyNotEquals(self):
        self.assertNotEqual(UpdateMany({'foo': 42}, {'$set': {'bar': 42}}),
                            UpdateMany({'foo': 42}, {'$set': {'bar': 23}}))

    def test_UpdateOneNotEqualsUpdateMany(self):
        self.assertNotEqual(UpdateOne({'foo': 42}, {'$set': {'bar': 42}}),
                            UpdateMany({'foo': 42}, {'$set': {'bar': 42}}))

if __name__ == "__main__":
    unittest.main()
