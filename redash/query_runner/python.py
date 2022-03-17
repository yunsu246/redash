import datetime
import importlib
import logging
import numpy as np
import pandas as pd
import sys

from redash.query_runner import *
from redash.utils import json_dumps, json_loads, collect_query_parameters, mustache_render
from redash import models
from RestrictedPython import compile_restricted
from RestrictedPython.Guards import safe_builtins, guarded_iter_unpack_sequence, guarded_unpack_sequence

try:
    import pandas as pd
    import numpy as np
    pandas_installed = True
except ImportError:
    pandas_installed = False

from RestrictedPython.transformer import IOPERATOR_TO_STR

logger = logging.getLogger(__name__)


def get_query(query_id):
    try:
        query = models.Query.get_by_id(query_id)
    except models.NoResultFound:
        raise Exception("Query id %s does not exist." % query_id)
    return query


class CustomPrint(object):
    """CustomPrint redirect "print" calls to be sent as "log" on the result object."""

    def __init__(self):
        self.enabled = True
        self.lines = []

    def write(self, text):
        if self.enabled:
            if text and text.strip():
                log_line = "[{0}] {1}".format(
                    datetime.datetime.utcnow().isoformat(), text
                )
                self.lines.append(log_line)

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False

    def __call__(self, *args):
        return self

    def _call_print(self, *objects, **kwargs):
        print(*objects, file=self)


class Python(BaseQueryRunner):
    should_annotate_query = False

    safe_builtins = (
        "sorted",
        "reversed",
        "map",
        "any",
        "all",
        "slice",
        "filter",
        "len",
        "next",
        "enumerate",
        "sum",
        "abs",
        "min",
        "max",
        "round",
        "divmod",
        "str",
        "int",
        "float",
        "complex",
        "tuple",
        "set",
        "list",
        "dict",
        "bool",
    )

    @classmethod
    def configuration_schema(cls):
        return {
            "type": "object",
            "properties": {
                "allowedImportModules": {
                    "type": "string",
                    "title": "Modules to import prior to running the script",
                },
                "additionalModulesPaths": {"type": "string"},
                "additionalBuiltins": {"type": "string"},
            },
        }

    @classmethod
    def enabled(cls):
        return True

    def __init__(self, configuration):
        super(Python, self).__init__(configuration)

        self.syntax = "python"

        self._allowed_modules = {}
        self._script_locals = {"result": {"rows": [], "columns": [], "log": []}}
        self._enable_print_log = True
        self._custom_print = CustomPrint()

        if self.configuration.get("allowedImportModules", None):
            for item in self.configuration["allowedImportModules"].split(","):
                self._allowed_modules[item] = None

        if self.configuration.get("additionalModulesPaths", None):
            for p in self.configuration["additionalModulesPaths"].split(","):
                if p not in sys.path:
                    sys.path.append(p)

        if self.configuration.get("additionalBuiltins", None):
            for b in self.configuration["additionalBuiltins"].split(","):
                if b not in self.safe_builtins:
                    self.safe_builtins += (b, )

    def custom_import(self, name, globals=None, locals=None, fromlist=(), level=0):
        if name in self._allowed_modules:
            m = None
            if self._allowed_modules[name] is None:
                m = importlib.import_module(name)
                self._allowed_modules[name] = m
            else:
                m = self._allowed_modules[name]

            return m

        raise Exception(
            "'{0}' is not configured as a supported import module".format(name)
        )

    @staticmethod
    def custom_write(obj):
        """
        Custom hooks which controls the way objects/lists/tuples/dicts behave in
        RestrictedPython
        """
        return obj

    @staticmethod
    def custom_get_item(obj, key):
        return obj[key]

    @staticmethod
    def custom_get_iter(obj):
        return iter(obj)

    @staticmethod
    def custom_inplacevar(op, x, y):
        if op not in IOPERATOR_TO_STR.values():
            raise Exception("'{} is not supported inplace variable'".format(op))
        glb = {"x": x, "y": y}
        exec("x" + op + "y", glb)
        return glb["x"]

    @staticmethod
    def add_result_column(result, column_name, friendly_name, column_type):
        """Helper function to add columns inside a Python script running in Redash in an easier way

        Parameters:
        :result dict: The result dict
        :column_name string: Name of the column, which should be consisted of lowercase latin letters or underscore.
        :friendly_name string: Name of the column for display
        :column_type string: Type of the column. Check supported data types for details.
        """
        if column_type not in SUPPORTED_COLUMN_TYPES:
            raise Exception("'{0}' is not a supported column type".format(column_type))

        if "columns" not in result:
            result["columns"] = []

        result["columns"].append(
            {"name": column_name, "friendly_name": friendly_name, "type": column_type}
        )

    @staticmethod
    def add_result_row(result, values):
        """Helper function to add one row to results set.

        Parameters:
        :result dict: The result dict
        :values dict: One row of result in dict. The key should be one of the column names. The value is the value of the column in this row.
        """
        if "rows" not in result:
            result["rows"] = []

        result["rows"].append(values)

    @staticmethod
    def execute_query(data_source_name_or_id, query, result_type=None):
        """Run query from specific data source.

        Parameters:
        :data_source_name_or_id string|integer: Name or ID of the data source
        :query string: Query to run
        """
        try:
            if type(data_source_name_or_id) == int:
                data_source = models.DataSource.get_by_id(data_source_name_or_id)
            else:
                data_source = models.DataSource.get_by_name(data_source_name_or_id)
        except models.NoResultFound:
            raise Exception("Wrong data source name/id: %s." % data_source_name_or_id)

        # TODO: pass the user here...
        data, error = data_source.query_runner.run_query(query, None)
        if error is not None:
            raise Exception(error)

        # TODO: allow avoiding the JSON dumps/loads in same process
        query_result = json_loads(data)

        if result_type == "dataframe" and pandas_installed:
            return pd.DataFrame(query_result["rows"])

        return query_result


    @staticmethod
    def get_source_schema(data_source_name_or_id):
        """Get schema from specific data source.

        :param data_source_name_or_id: string|integer: Name or ID of the data source
        :return:
        """
        try:
            if type(data_source_name_or_id) == int:
                data_source = models.DataSource.get_by_id(data_source_name_or_id)
            else:
                data_source = models.DataSource.get_by_name(data_source_name_or_id)
        except models.NoResultFound:
            raise Exception("Wrong data source name/id: %s." % data_source_name_or_id)
        schema = data_source.query_runner.get_schema()
        return schema

    @staticmethod
    def get_query_result(query_id):
        """Get result of an existing query.

        Parameters:
        :query_id integer: ID of existing query
        """
        try:
            query = models.Query.get_by_id(query_id)
        except models.NoResultFound:
            raise Exception("Query id %s does not exist." % query_id)

        if query.latest_query_data is None:
            raise Exception("Query does not have results yet.")

        if query.latest_query_data.data is None:
            raise Exception("Query does not have results yet.")

        return query.latest_query_data.data

    def dataframe_to_result(self, result, df):

        result["rows"] = df.to_dict("records")

        for column_name, column_type in df.dtypes.items():
            if column_type == np.bool:
                redash_type = TYPE_BOOLEAN
            elif column_type == np.inexact:
                redash_type = TYPE_FLOAT
            elif column_type == np.integer:
                redash_type = TYPE_INTEGER
            elif column_type in (np.datetime64, np.dtype('<M8[ns]')):
                if df.empty:
                    redash_type = TYPE_DATETIME
                elif len(df[column_name].head(1).astype(str).loc[0]) > 10:
                    redash_type = TYPE_DATETIME
                else:
                    redash_type = TYPE_DATE
            else:
                redash_type = TYPE_STRING

            self.add_result_column(result, column_name, column_name, redash_type)

    @staticmethod
    def execute_query_by_id(query_id, params=None):
        """Run query from specific query_id.
        Parameters:
        :query_id int: Query id to run
        :params dict: Params for bind to query
        """
        query = get_query(query_id)
        query_text = query.query_text
        query_params = set(collect_query_parameters(query_text))
        if params is None:
            query_text = query.query_text
            missing_params = set(query_params)
        else:
            query_text = mustache_render(query.query_text, params)
            missing_params = set(query_params) - set(params.keys())
        if len(missing_params) > 0:
            raise Exception('Missing parameter value for: {}'.format(", ".join(missing_params)))
        data, error = query.data_source.query_runner.run_query(query_text, None)
        if error is not None:
            raise Exception(error)
        return json_loads(data)
    
    @staticmethod
    def df_to_redash(df_orig, index_to_col=False):
        df = df_orig.copy()
        if index_to_col:
            df.reset_index(inplace=True)
        result = {'columns': [], 'rows': []}
        conversions = [
            {'pandas_type': np.integer, 'redash_type': 'integer',},
            {'pandas_type': np.inexact, 'redash_type': 'float',},
            {'pandas_type': np.datetime64, 'redash_type': 'datetime', 'to_redash': lambda x: x.strftime('%Y-%m-%d %H:%M:%S')},
            {'pandas_type': np.bool_, 'redash_type': 'boolean'},
            {'pandas_type': np.object, 'redash_type': 'string'}
        ]
        labels = []
        for dtype, label in zip(df.dtypes, df.columns):
            for conversion in conversions:
                if issubclass(dtype.type, conversion['pandas_type']):
                    result['columns'].append({'name': label, 'friendly_name': label, 'type': conversion['redash_type']})
                    labels.append(label)
                    func = conversion.get('to_redash')
                    if func:
                        df[label] = df[label].apply(func)
                    break
        result['rows'] = df[labels].replace({np.nan: None}).to_dict(orient='records')
        return result
    
    @staticmethod    
    def redash_to_df(result, col_to_index=False):
        conversions = [
            {'redash_type': 'datetime', 'to_pandas': lambda x: pd.to_datetime(x, infer_datetime_format=True)},
            {'redash_type': 'date', 'to_pandas': lambda x: pd.to_datetime(x, infer_datetime_format=True)},
        ]
        df = pd.DataFrame.from_dict(result['rows'], orient='columns')
        labels = []
        for column in result['columns']:
            label = column['name']
            labels.append(label)
            for conversion in conversions:
                if conversion['redash_type'] == column['type']:
                    func = conversion.get('to_pandas')
                    if func:
                        df[label] = df[label].apply(func)
                    break
        df = df[labels]
        if col_to_index and labels:
            df.set_index(labels[0], inplace=True)
        return df

    def get_current_user(self):
        return self._current_user.to_dict()

    def test_connection(self):
        pass

    def run_query(self, query, user):
        self._current_user = user

        try:
            error = None

            code = compile_restricted(query, "<string>", "exec")

            builtins = safe_builtins.copy()
            builtins["_write_"] = self.custom_write
            builtins["__import__"] = self.custom_import
            builtins["_getattr_"] = getattr
            builtins["getattr"] = getattr
            builtins["_setattr_"] = setattr
            builtins["setattr"] = setattr
            builtins["_getitem_"] = self.custom_get_item
            builtins["_getiter_"] = self.custom_get_iter
            builtins["_print_"] = self._custom_print
            builtins["_unpack_sequence_"] = guarded_unpack_sequence
            builtins["_iter_unpack_sequence_"] = guarded_iter_unpack_sequence
            builtins["_inplacevar_"] = self.custom_inplacevar

            # Layer in our own additional set of builtins that we have
            # considered safe.
            for key in self.safe_builtins:
                builtins[key] = __builtins__[key]

            restricted_globals = dict(__builtins__=builtins)
            restricted_globals["get_query_result"] = self.get_query_result
            restricted_globals["get_source_schema"] = self.get_source_schema
            restricted_globals["get_current_user"] = self.get_current_user
            restricted_globals["execute_query"] = self.execute_query
            restricted_globals["execute_query_by_id"] = self.execute_query_by_id
            restricted_globals["add_result_column"] = self.add_result_column
            if pandas_installed:
                restricted_globals["dataframe_to_result"] = self.dataframe_to_result
                restricted_globals["df_to_redash"] = self.df_to_redash
                restricted_globals["redash_to_df"] = self.redash_to_df
            restricted_globals["add_result_row"] = self.add_result_row
            restricted_globals["disable_print_log"] = self._custom_print.disable
            restricted_globals["enable_print_log"] = self._custom_print.enable

            # Supported data types
            restricted_globals["TYPE_DATETIME"] = TYPE_DATETIME
            restricted_globals["TYPE_BOOLEAN"] = TYPE_BOOLEAN
            restricted_globals["TYPE_INTEGER"] = TYPE_INTEGER
            restricted_globals["TYPE_STRING"] = TYPE_STRING
            restricted_globals["TYPE_DATE"] = TYPE_DATE
            restricted_globals["TYPE_FLOAT"] = TYPE_FLOAT

            # TODO: Figure out the best way to have a timeout on a script
            #       One option is to use ETA with Celery + timeouts on workers
            #       And replacement of worker process every X requests handled.

            exec(code, restricted_globals, self._script_locals)

            result = self._script_locals["result"]
            result["log"] = self._custom_print.lines
            json_data = json_dumps(result)
        except Exception as e:
            error = str(type(e)) + " " + str(e)
            json_data = None

        return json_data, error

register(Python)
