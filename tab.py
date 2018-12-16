#!/usr/bin/env python
import sys
import io
import tabulator

with open(0, 'rb') as f:
    data = io.BytesIO(f.read())

    with tabulator.Stream(data, format="csv") as stream:
        print(stream.headers)
        print(stream.sample)
        for row in stream.iter():
            print(row)

        stream.reset()
        rows = stream.read()
