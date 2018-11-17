import sys
import argparse
import csv
import sqlite3

from pytablewriter import TableWriterFactory

from tql.exceptions import Error
from tql.filters import print_filter_list_table, preprocess_filters, apply_filters
from tql.output import do_output
from tql.replacements import print_replacements_table
from tql.sql import rewrite_sql, process_table_remapping, process_column_remapping
from tql.utils import error, expand_path_and_exists

DEBUG = False


def debug(s, title=None):
    if DEBUG:
        sys.stderr.write(f"{title or ''}{s!r}\n")


def main(args=None):
    global DEBUG
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser()
    parser.add_argument('sql', nargs='*', help="The SQL to execute. "
                                               "Use filenames surrounded by single or double quotes to specify CSV sources instead of existing tables in the FROM clause(s). "
                                               "You can use [:...:] replacements for special characters (see --help-filters for more information.")
    parser.add_argument('--dialect', '-t', choices=csv.list_dialects(), default='unix',
                        help=f"Specify the CSV dialect. Valid values are {', '.join(csv.list_dialects())}. Default is `unix`.")
    parser.add_argument('--delimiter', '-d', default=',', help="Specify the CSV delimiter to use. Default is a comma (,).")
    parser.add_argument('--quotechar', '--quote-char', '-q', default='"', help='Specify the CSV quote charactor. Default is double quote (").')
    parser.add_argument('--output', '-o', default='-', help="Output file. Default is stdout (-).")
    parser.add_argument('--output-format', '--out-format', '--out-fmt', '-f', default='table', choices=TableWriterFactory.get_format_name_list() + ['table', 'ptable', 'pt'],
                        help="Output format. Valid value are 'table' and 'csv'. Default is table.")

    db_group = parser.add_mutually_exclusive_group()
    db_group.add_argument('--save-db', '-s', help="Specify a SQLite database to use (instead of using an in-memory database. The database will remain after tql exits.")
    db_group.add_argument('--load-db', '-l', help="Load an existing database instead of creating a new one.")

    parser.add_argument('--skip-lines', '--skip', '-k', type=int, default=0, help="Skip `SKIP_LINES` lines at the beginning of the file. Default is 0.")
    parser.add_argument('--headers', '-r',
                        help="Don't use the first non-skipped line for header/column names, use these header/column names instead. "
                             "Format is a comma separated list of column names. "
                             "Column names must not be SQLite reserved words.")
    parser.add_argument('--debug', '-g', action='store_true', help="Turn on debug output.")
    parser.add_argument('--filter', '-e', action='append',
                        help="Specify a column filter. Use one filter per switch/param. "
                             "Format is <column_name>|filter|<0 or more params or additional filters in filter chain>.  "
                             "Filters have a variable number of parameters (0+). Filters may be chained.")
    parser.add_argument('--auto-filter', '-a', action='store_true', help="Automatically apply the `num` filter to all column data.")
    parser.add_argument('--filters-list', '--filter-list', '--help-filters', action='store_true')
    parser.add_argument('--replacements-list', '--replacement-list', '--help-replacements', action='store_true')
    parser.add_argument('--remap-column', '--remap-header', '-m', action='append',
                        help="A single column re-map in the form <col_name>=<new_col_name>. Use one switch for each column re-mapping. "
                             "This overrides any column/header names that are auto-discovered or passed in via --headers/-r. "
                             "You can use [:...:] replacements for special characters (see --help-filters for more information.")
    parser.add_argument('--remap-table', '--remap-file', '-T', action='append',
                        help="A single table re-map in the form <table_name>=<new_table_name>. Use one switch for each table re-mapping. "
                             "This overrides any table names that are auto-generated from filenames passed in via the SQL statement. "
                             "You can use [:...:] replacements for special characters (see --help-filters for more information.")
    #parser.add_argument('--merge-columns', '--merge', '-M')  # -M "one,two,three=foo"
    #parser.add_argument('--split-column', '--split', '-S')  # -S "foo=one,two,three"

    # TODO: Handle more CSV parser params
    # TODO: Handle duplicate column names (in -r)
    # TODO: Modification queries? (read CSV, apply filters, save to db, apply SQL modification(s), output new CSV)
    # TODO: Auto filtering to number with a switch? (only for columns w/o an explicit filter with -e)
    # IDEA: Load from markdown table?
    # IDEA: Load from URL? Save CSV to URL?
    # REVISIT: Maybe use a diff. character after the filter name and/or between params? c1|replace:foo,bar|lower|...

    args = parser.parse_args(args=args)
    DEBUG = args.debug
    debug(args, 'args=')

    if args.filters_list:
        print_filter_list_table(args.output_format)
        return 0

    if args.replacements_list:
        print_replacements_table(args.output_format)
        return 0

    if not args.sql:
        raise Error("You must specify the SQL to execute.")

    # Process table re-mappings, if any
    table_remapping = process_table_remapping(args.remap_table)
    debug(table_remapping, 'table_remapping=')

    # Re-write the SQL, replacing filenames with table names and apply table re-mapping(s)
    sql, tables = rewrite_sql(args.sql, table_remapping)
    debug(sql, 'sql=')
    debug(tables, 'tables=')

    # Pre-process the filters
    filters = preprocess_filters(args.filter)
    debug(filters, 'filters=')

    # Process the column re-mappings, if any
    column_remapping = process_column_remapping(args.remap_column)
    debug(column_remapping, 'column_remapping=')

    # Open the database
    if args.save_db:
        path, exists = expand_path_and_exists(args.save_db)
        if exists:
            raise Error("fDatabase file {path} already exists.")
        con = sqlite3.connect(path)
    elif args.load_db:
        path, exists = expand_path_and_exists(args.load_db)
        if not exists:
            raise FileNotFoundError(f"Database file {path} not found.")
        con = sqlite3.connect(path)
    else:
        con = sqlite3.connect(":memory:")

    cur = con.cursor()

    # Read each CSV or TSV file and insert into a SQLite table based on the filename of the file
    for tablename, path in tables.items():
        with open(path) as f:
            if args.skip_lines:
                [f.readline() for _ in range(args.skip_lines)]

            reader = csv.reader(f, dialect=args.dialect, delimiter=args.delimiter, quotechar=args.quotechar)
            first, colnames = True, []

            for row in reader:
                debug(row)
                row = [n.strip() for n in row if n]

                if first:
                    placeholders = ', '.join(['?'] * len(row))
                    col_src = args.headers.split(',') if args.headers else row
                    colnames = [column_remapping.get(n.strip()) or n.strip() for n in col_src]

                    # Apply auto filtering
                    if args.auto_filter:
                        for col in colnames:
                            if col not in filters:
                                filters[col] = [['num']]
                        debug(filters, 'filters (auto)=')

                    debug(colnames, 'colnames=')
                    colnames_str = ','.join(colnames)

                    # TODO: For -l/--load-db, handle case where db has an existing table with same name as one of the CSV input table(s)
                    s = f"CREATE TABLE {tablename} ({colnames_str});"
                    debug(s, 'table create: ')
                    try:
                        cur.execute(s)
                    except sqlite3.OperationalError as e:
                        raise Error("Failed to create table. Most likely cause is missing headers. "
                                    "Use --headers/-r and/or --skip-lines/-k to setup headers.")

                    first = False
                    continue

                filtered_row = apply_filters(filters, colnames, row)
                debug(row, 'row=')
                s = f"INSERT INTO {tablename} ({colnames_str}) VALUES ({placeholders});"
                print(s, filtered_row)
                cur.execute(s, filtered_row)

    con.commit()

    debug(sql, 'sql=')
    do_output(sql, cur, args.output, args.output_format, args.delimiter)
    con.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Error as e:
        error(e)
        sys.exit(1)  # TODO: correct result code
