"""Import a local CSV/JSON history manifest for V1 replay."""
from __future__ import annotations

import argparse
import csv
from hashlib import sha256
import io
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from v1_backtest_data import _reject_constant, _unique_object, demo_bundle, validate_bundle


PRICE = ('open', 'high', 'low', 'close')
DAILY = ('date', 'code', 'type', *PRICE, 'volume')
SESSION = ('date', 'code', 'status', 'corporate_action_clear')
MINUTE = ('date', 'code', 'time', *PRICE, 'volume')
FILES = ('calendar', 'daily', 'sessions', 'minutes', 'reviews')


def read_json(raw):
    return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def csv_rows(raw, fields, label):
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
    columns = reader.fieldnames or []
    if len(columns) != len(set(columns)) or not set(fields).issubset(columns):
        raise ValueError(f'{label} 缺少必要列或列名重复：{", ".join(fields)}')
    for row in reader:
        if None in row or None in row.values():
            raise ValueError(f'{label} 第 {reader.line_num} 行列数不一致')
        yield {key: value.strip() for key, value in row.items()}


def boolean(value, label):
    if value not in ('true', 'false'):
        raise ValueError(f'{label} 须为 true 或 false')
    return value == 'true'


def candle(row):
    return {key: float(row[key]) for key in PRICE}


def read_inputs(path):
    manifest_bytes = path.read_bytes()
    manifest = read_json(manifest_bytes)
    if not isinstance(manifest, dict) or type(manifest.get('version')) is not int or manifest['version'] != 1:
        raise ValueError('输入清单版本须为 1')
    if manifest.get('benchmark_code') != '000300' or manifest.get('price_basis') != 'raw':
        raise ValueError('基准须为沪深 300（000300），价格口径须为 raw')
    names = (*FILES, 'universe') if 'universe' in manifest else FILES
    inputs = {}
    for name in names:
        filename = manifest.get(name)
        if not isinstance(filename, str) or not filename or Path(filename).is_absolute():
            raise ValueError(f'{name} 需指定清单目录下的相对路径')
        source = (path.parent / filename).resolve()
        if not source.is_relative_to(path.parent):
            raise ValueError(f'{name} 文件须位于清单目录下')
        inputs[name] = source.read_bytes()
    digests = {'manifest': sha256(manifest_bytes).hexdigest(),
               **{name: sha256(raw).hexdigest() for name, raw in inputs.items()}}
    return manifest, inputs, digests


def import_history(manifest_path):
    path = Path(manifest_path).resolve()
    manifest, inputs, digests = read_inputs(path)
    bundle = {key: manifest[key] for key in ('version', 'price_basis')}
    bundle.update(source=manifest.get('source'), calendar_source=manifest.get('calendar_source'),
                  input_digests=digests, benchmark=[], securities={})
    if 'demo' in manifest:
        bundle['demo'] = manifest['demo']
    bundle['calendar'] = sorted(row['date'] for row in csv_rows(inputs['calendar'], ('date',), 'calendar'))
    securities = bundle['securities']
    for row in csv_rows(inputs['daily'], DAILY, 'daily'):
        code, kind = row['code'], row['type']
        bar = {'date': row['date'], **candle(row), 'volume': float(row['volume'])}
        if row.get('complete'):
            bar['complete'] = boolean(row['complete'], 'daily.complete')
        if code == manifest['benchmark_code']:
            if kind != 'index':
                raise ValueError('沪深 300 日线的 type 须为 index')
            bundle['benchmark'].append(bar)
        else:
            if not row.get('amount'):
                raise ValueError(f'{code} {row["date"]} 缺少日成交额 amount')
            bar['amount'] = float(row['amount'])
            security = securities.setdefault(code, {'type': kind, 'daily': [], 'sessions': {}, 'reviews': {}})
            if kind != security['type']:
                raise ValueError(f'{code} 出现不同证券类型')
            security['daily'].append(bar)
    if not bundle['benchmark']:
        raise ValueError('daily 文件缺少沪深 300 日线')
    for bars in (bundle['benchmark'], *(item['daily'] for item in securities.values())):
        bars.sort(key=lambda bar: bar['date'])

    for row in csv_rows(inputs['sessions'], SESSION, 'sessions'):
        code, day = row['code'], row['date']
        if code not in securities or day in securities[code]['sessions']:
            raise ValueError(f'{code} {day} 缺少证券日线或有重复场次')
        session = {'status': row['status'],
                   'corporate_action_clear': boolean(row['corporate_action_clear'], 'sessions.corporate_action_clear')}
        for field in ('open', 'upper_limit', 'lower_limit'):
            if row.get(field):
                session[field] = float(row[field])
        if session['status'] == 'open':
            session['minutes'] = []
        securities[code]['sessions'][day] = session
    for row in csv_rows(inputs['minutes'], MINUTE, 'minutes'):
        code, day = row['code'], row['date']
        session = securities.get(code, {}).get('sessions', {}).get(day, {})
        if session.get('status') != 'open':
            raise ValueError(f'{code} {day} 的分钟记录缺少对应开市场次')
        session['minutes'].append({'time': row['time'], **candle(row), 'volume': int(row['volume'])})
    for item in securities.values():
        for session in item['sessions'].values():
            if session['status'] == 'open':
                session['minutes'].sort(key=lambda bar: bar['time'])

    reviews = read_json(inputs['reviews'])
    if not isinstance(reviews, dict) or any(code not in securities for code in reviews):
        raise ValueError('reviews 须按已导入的证券代码和信号日组织')
    for code, dates in reviews.items():
        securities[code]['reviews'] = dates

    if 'universe' in inputs:
        pool = {}
        empty_days = set()
        for row in csv_rows(inputs['universe'], ('date', 'code'), 'universe'):
            day, code = row['date'], row['code']
            if day in empty_days or (day in pool and not code):
                raise ValueError(f'{day} 空证券名单记录重复或与证券代码冲突')
            members = pool.setdefault(day, [])
            if code:
                members.append(code)
            else:
                empty_days.add(day)
        bundle.update(universe=pool, universe_source=manifest.get('universe_source'))
    return validate_bundle(bundle)


def write_bundle(bundle, path):
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise FileExistsError(f'历史数据包已存在：{path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(bundle, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write('\n')
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_demo_manifest(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    bundle = demo_bundle()
    manifest = {key: bundle[key] for key in ('version', 'source', 'calendar_source', 'price_basis', 'demo')}
    manifest.update(benchmark_code='000300', **{key: key + '.csv' for key in FILES if key != 'reviews'},
                    reviews='reviews.json', universe='universe.csv', universe_source='合成信号日名单')

    def write_csv(name, fields, rows):
        with (directory / manifest[name]).open('x', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    write_csv('calendar', ('date',), ({'date': day} for day in bundle['calendar']))
    daily = [dict(row, code='000300', type='index') for row in bundle['benchmark']]
    sessions, minutes, reviews = [], [], {}
    for code, item in bundle['securities'].items():
        daily.extend(dict(row, code=code, type=item['type']) for row in item['daily'])
        reviews[code] = item['reviews']
        for day, source in item['sessions'].items():
            session = {key: value for key, value in source.items() if key != 'minutes'}
            session['corporate_action_clear'] = str(session['corporate_action_clear']).lower()
            sessions.append(dict(session, date=day, code=code))
            minutes.extend(dict(bar, date=day, code=code) for bar in source.get('minutes', []))
    write_csv('daily', (*DAILY, 'amount', 'complete'), daily)
    write_csv('sessions', (*SESSION, 'open', 'upper_limit', 'lower_limit'), sessions)
    write_csv('minutes', MINUTE, minutes)
    write_csv('universe', ('date', 'code'), (
        {'date': day, 'code': code}
        for day in bundle['calendar'][-4:-1] for code in bundle['securities']
    ))
    write_bundle(reviews, directory / manifest['reviews'])
    path = directory / 'manifest.json'
    write_bundle(manifest, path)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--manifest', type=Path, help='行情文件清单 JSON')
    source.add_argument('--write-demo', type=Path, help='在一个新目录生成合成输入样例')
    parser.add_argument('--output', type=Path, help='输出历史数据包 JSON')
    args = parser.parse_args()
    if bool(args.manifest) != bool(args.output):
        parser.error('--manifest 须与 --output 一起使用')
    try:
        if args.write_demo:
            print(write_demo_manifest(args.write_demo))
        else:
            write_bundle(import_history(args.manifest), args.output)
            print(args.output)
    except (OSError, ValueError, csv.Error) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
