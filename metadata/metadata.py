import json
from pyexcel_ods3 import get_data
import isbn
import argparse
import urllib.request

parser = argparse.ArgumentParser(description="Tool for dealing with metadata")
parser.add_argument("-f", "--file", help="metadata ods file path", required=True)
parser.add_argument("-o", "--output", help="output json file path", default="metadata.json")
parser.add_argument("-s", "--size", help="filesize json file url", required=True)
args = parser.parse_args()

if args.file:
    data = get_data(args.file)
    result = []
    failed = 0
    success = {
        "books": 0,
        "tests": 0,
        "docs": 0,
    }
    # ['MD5校验码', '书名', '作者/编者', '译者', '版次/出版年份', '出版社', 'ISBN码', '文件类型', '备注']
    for i in data['books'][1:]:
        if all([x == "" for x in i]):
            continue
        if len(i) < 8:
            print("[!] Invalid book: ", i)
            failed += 1
            continue
        if not isbn.ISBN.valid(i[6]):
            print("[!] Invalid ISBN: ", i[6])
            print(f"\tbooks: {i[1]} {i[0]}")
            failed += 1
            continue
        result.append({
            "type":  "book",
            "data": {
                "title": i[1],
                "authors": list(filter(lambda x: x != '', i[2].split('/'))),
                "translators": list(filter(lambda x: x != '', i[3].split('/'))),
                "edition": i[4],
                "publisher": i[5],
                "isbn": isbn.ISBN(i[6]).hyphen(),
                "filetype": i[7],
                "md5": i[0],
            }
        })
        success["books"] += 1

    # ['MD5校验码', '课程类型', '学院', '年份', '课程名称', '考试阶段（期中0/期末1）', '资料类型（纯试题Q/纯答案A）', '文件类型', '备注']
    for i in data['tests'][1:]:
        if all([x == "" for x in i]):
            continue
        if len(i) < 8:
            print("[!] Invalid test: ", i)
            failed += 1
            continue
        if str(i[5]) != "0" and str(i[5]) != "1" and str(i[5]) != "":
            print("[!] Invalid stage: ", i[5])
            print(f"\ttest: {i[4]} {i[0]}")
            failed += 1
            continue
        item = {
            "type":  "test",
            "data": {
                "college": i[2] if i[2] != "" else None,
                "course": {
                    "type": i[1] if i[1] != "" else None,
                    "name": i[4] if i[4] != "" else None,
                },
                "filetype": str(i[7]).lower(),
                "stage": "期中" if str(i[5]) == "0" else "期末" if str(i[5]) == "1" else None,
                "content": "试题" if i[6] == "Q" else "答案" if i[6] == "A" else "试题+答案" if i[6] == "QA" else "未知",
                "md5": i[0],
                "time": str(i[3]) if i[3] != "" else None,
            }
        }
        if item["data"]["time"] and item["data"]["time"].count('-') == 2:
            start, end, period = item["data"]["time"].split('-')
            if period != "1" and period != "2":
                print("[!] Invalid period: ", item["data"]["time"])
                print(f"\ttest: {item['data']['course']['name']} {item['data']['md5']}")
                failed += 1
                continue
            item["data"]["time"] = f"{start}-{end} 第{'一' if period == '1' else '二'}学期"
        
        item["data"]['title'] = f"{item['data']['time'] + ' ' if item['data']['time'] else ''}{item['data']['course']['name'] or '未知'}{' ' + item['data']['stage'] if item['data']['stage'] else ''}"
        result.append(item)
        success["tests"] += 1

    # ['MD5校验码', '课程类型', '课程名称', '文件名称', '资料类型（思维导图M/题库Q/答案A/知识点K/课件C）', '文件类型', '备注']
    for i in data['docs'][1:]:
        if all([x == "" for x in i]):
            continue
        if len(i) < 6:
            print("[!] Invalid doc: ", i)
            failed += 1
            continue
        for c in i[4]:
            if c != "M" and c != "Q" and c != "A" and c != "K" and c != "C":
                print("[!] Invalid content: ", i[4])
                print(f"\tdoc: {i[3]} {i[0]}")
                failed += 1
                continue

        result.append({
            "type":  "doc",
            "data": {
                "title": i[3],
                "filetype": i[5],
                "md5": i[0],
                "course": {
                    "type": i[1] if i[1] != "" else None,
                    "name": i[2] if i[2] != "" else None,
                },
                "content": [
                    "思维导图" if c == "M" else "题库" if c == "Q" else "答案" if c == "A" else "知识点" if c == "K" else "课件" if c == "C" else "未知"
                    for c in i[4]
                ]
            }
        })
        success["docs"] += 1

    if failed != 0:
        print(f"[!] {failed} items failed to parse.")

    print(f"[+] {len(result)} items parsed ({success['books']} books, {success['tests']} tests, {success['docs']} docs).")

filesizes = {}
try:
    with urllib.request.urlopen(args.size) as response:
        data = json.loads(response.read().decode("utf-8"))
        for item in data:
            filesizes[item["filename"]] = item["size"]
except:
    print("[!] Failed to load filesize file.")

if args.file:
    for item in result:
        item['data']['filesize'] = filesizes.get(f'{item["data"]["md5"]}.{item["data"]["filetype"]}', None)

    with open(args.output, "w") as f:
        f.write(json.dumps(result, ensure_ascii=False,separators=(',', ':')))