import asyncio
import json
import os
import re
import time
from datetime import datetime

import aiohttp
import requests
import urllib3
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common import WebDriverException
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# Chrome 调试端口，Chrome 启动时要加参数 --remote-debugging-port=9222
port = 9222
# PC web 端，用户id
profile_id = ''
# 小红书 cookie，有助于防ban，最好用小号！
xhs_cookie = ''
# 并发量
semaphore_of_parse = 2
semaphore_of_parse_when_note_less_than_100 = 5
semaphore_of_download = 10

options = Options()
caps = {
    'browserName': 'chrome',
    'goog:loggingPrefs': {'performance': 'ALL'}
}
for key, value in caps.items():
    options.set_capability(key, value)
options.add_experimental_option('debuggerAddress', f'127.0.0.1:{port}')
browser = webdriver.Chrome(options=options)
browser.implicitly_wait(10)


def open_browser_and_login():
    browser.execute_script("window.open('');")
    browser.switch_to.window(browser.window_handles[-1])
    browser.get(f'https://www.xiaohongshu.com/user/profile/{profile_id}')
    time.sleep(2)


def find_result_in_first_page() -> dict:
    page_content = browser.page_source
    soup = BeautifulSoup(page_content, 'html.parser')
    scripts = soup.findAll('script')
    result = None
    for script in scripts:
        if script.string is not None and 'window.__INITIAL_STATE__=' in script.string:
            try:
                result = json.loads(script.string.replace('window.__INITIAL_STATE__=', '').replace('undefined', 'null'))
            except TypeError:
                print(f'json解析错误')
            break
    return result


def scroll_one_screen():
    body = browser.find_element(By.TAG_NAME, 'body')
    delta_height = body.rect['height']
    ActionChains(browser) \
        .scroll_by_amount(0, delta_height) \
        .perform()
    time.sleep(2)


def recursion_scroll_until_no_more(note_id_list: list, cursor: str) -> list:
    if cursor is None and cursor != '':
        return []
    print(f'正在进行递归获取分页数据，滚动10次并拦截请求')
    # 向下滚动页面10次
    for _ in range(10):
        scroll_one_screen()
    # 拦截 https://edith.xiaohongshu.com/api/sns/web/v1/user_posted 请求
    last_recursion_data = None
    performance_log = browser.get_log('performance')
    for packet in performance_log:
        message = json.loads(packet.get('message')).get('message')
        if message.get('method') != 'Network.responseReceived':
            continue
        packet_type = message.get('params').get('response').get('mimeType')
        if packet_type != 'application/json':
            continue
        request_id = message.get('params').get('requestId')
        url = message.get('params').get('response').get('url')
        if 'https://edith.xiaohongshu.com/api/sns/web/v1/user_posted' not in url:
            continue
        try:
            resp = browser.execute_cdp_cmd('Network.getResponseBody', {'requestId': request_id})  # selenium调用 cdp
            recursion_data = json.loads(resp['body'])['data']

            note_id_list = note_id_list + [{
                'noteId': item['note_id'],
                'xsecToken': item['xsec_token'],
                'displayTitle': item['display_title']
            } for item in recursion_data['notes']]
            last_recursion_data = recursion_data
        except WebDriverException as e:
            print(e.msg)
            continue
    if last_recursion_data is not None and last_recursion_data['cursor'] is not None and last_recursion_data['cursor'] != '':
        print(f'游标不为空，继续递归')
        recursion_scroll_until_no_more(note_id_list, last_recursion_data['cursor'])
    return note_id_list


def parse_note_by_note_id(parsed_note_info_list: list, note_id: str, xsec_token: str, note_index: int = 0):
    urllib3.disable_warnings()
    response = requests.get(f'https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}&xsec_source=pc_user', verify=False)
    if response.status_code == 200:
        html_text = response.text
        parse_html_text(parsed_note_info_list, html_text, note_id, note_index)
    else:
        raise Exception(f'请求返回异常，{response.status_code}')


def parse_html_text(parsed_note_info_list: list, html_text: str, note_id: str, note_index: int):
    soup = BeautifulSoup(html_text, 'html.parser')
    scripts = soup.findAll('script')
    result = None
    for script in scripts:
        if script.string is not None and 'window.__INITIAL_STATE__=' in script.string:
            try:
                result = json.loads(script.string.replace('window.__INITIAL_STATE__=', '').replace('undefined', 'null'))
            except TypeError:
                print(f'json解析错误，noteId：{note_id}')
            break
    if result is None:
        print('没有找到window.__INITIAL_STATE__，疑似被盾，稍等一会儿再试')
        return
    note = result['note']['noteDetailMap'][result['note']['firstNoteId']]['note']
    user_id = note['user']['userId']
    user_name = note['user']['nickname']
    create_time = datetime.fromtimestamp(note['time'] / 1000).strftime('%Y-%m-%d %H%M%S')
    title = note['title']
    desc = note['desc']
    image_id_list = [i['infoList'][0]['url'].split('!')[0].split('/').pop() for i in note['imageList']]
    video = note.get('video')
    video_url = None
    if video is not None:
        stream = video['media']['stream']
        match = re.search(r'"videoCodec": "(.*?)"', json.dumps(stream))
        if match:
            video_url = stream[match.group(1)][0]['masterUrl']
    parsed_note_info_list.append({
        'note_index': note_index,
        'note_id': note_id,
        'user_id': user_id,
        'user_name': user_name,
        'create_time': create_time,
        'title': title,
        'desc': desc,
        'image_id_list': image_id_list,
        'video_url': video_url,
    })


async def parse_with_aiohttp(sem: asyncio.Semaphore, parsed_note_info_list: list, parse_task_info_item: dict, client: aiohttp.ClientSession):
    note_id = parse_task_info_item['note_id']
    xsec_token = parse_task_info_item['xsec_token']
    note_index = parse_task_info_item['note_index']
    async with sem:
        async with client.get(f'https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}&xsec_source=pc_user', verify_ssl=False) as response:
            if response.status == 200:
                html_text = await response.text()
                await asyncio.sleep(0)
                if html_text:
                    parse_html_text(parsed_note_info_list, html_text, note_id, note_index)
                print(f'第{note_index}篇笔记解析成功')
            else:
                print(f'第{note_index}篇笔记解析失败❌，{response.status}')


def get_headers() -> dict:
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'zh-CN,zh;q=0.9',
        'cache-control': 'no-cache',
        'pragma': 'no-cache',
        'sec-ch-ua': '"Google Chrome";v="119", "Chromium";v="119", "Not?A_Brand";v="24"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"macOS"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'none',
        'sec-fetch-user': '?1',
        'upgrade-insecure-requests': '1'
    }
    if xhs_cookie is not None and xhs_cookie != '':
        headers['cookie'] = xhs_cookie
    return headers


async def async_parse_main(parsed_note_info_list: list, parse_task_info_list: list):
    sem = asyncio.Semaphore(semaphore_of_parse_when_note_less_than_100 if len(parse_task_info_list) < 100 else semaphore_of_parse)
    async with aiohttp.ClientSession(headers=get_headers()) as client:
        tasks = [parse_with_aiohttp(sem, parsed_note_info_list, item, client) for item in parse_task_info_list]
        return await asyncio.gather(*tasks)


def get_parsed_note_info_list(note_id_list: list) -> list:
    # 根据第一篇笔记获取用户名
    temp_list = []
    parse_note_by_note_id(temp_list, note_id_list[0]['noteId'], note_id_list[0]['xsecToken'])
    if not temp_list:
        raise Exception('temp_list 不应为空')
    user_id = temp_list[0]['user_id']
    user_name = temp_list[0]['user_name']

    base_path = f'download/{user_id}-{user_name}'
    os.makedirs(base_path, exist_ok=True)
    with open(f'{base_path}/{user_id}', 'w') as f:
        f.write(f'{user_id}')

    result_file_path = f'{base_path}/result.json'
    if os.path.isfile(result_file_path):
        print(f'{result_file_path}已存在，开始对比数据')
        with open(result_file_path, 'r') as f:
            parsed_note_info_list = json.loads(f.read())
        exist_node_id_list = [item.get('note_id') for item in parsed_note_info_list]
        not_exist_node_id_list = []
        for item in note_id_list:
            if item['noteId'] not in exist_node_id_list:
                not_exist_node_id_list.append(item)
        if not not_exist_node_id_list:
            print(f'无需重新获取{result_file_path}数据')
        else:
            print(f'以下笔记不存在于{result_file_path}，需要扩展获取：{not_exist_node_id_list}')
            parsed_note_info_list = get_result_json(result_file_path, not_exist_node_id_list, parsed_note_info_list)
    else:
        print(f'{result_file_path}不存在，获取并保存')
        parsed_note_info_list = get_result_json(result_file_path, note_id_list)

    return parsed_note_info_list


def get_result_json(result_file_path: str, note_id_list: list, parsed_note_info_list: list = None) -> list:
    if parsed_note_info_list is None:
        parsed_note_info_list = []
    print('开始获取json')
    parse_task_info_list = []
    note_count = len(parsed_note_info_list)
    for item in note_id_list:
        note_count = note_count + 1
        parse_task_info_list.append({
            'note_id': item['noteId'],
            'xsec_token': item['xsecToken'],
            'note_index': note_count
        })
    asyncio.run(async_parse_main(parsed_note_info_list, parse_task_info_list))
    parsed_note_info_list = sorted(parsed_note_info_list, key=lambda x: x['note_index'])
    try:
        os.remove(result_file_path)
    except FileNotFoundError:
        pass
    with open(result_file_path, 'w', encoding='UTF-8') as f:
        f.write(json.dumps(parsed_note_info_list, ensure_ascii=False))
        f.flush()
    print('获取json完毕')
    return parsed_note_info_list


def download_note(note: dict):
    user_id = note.get('user_id')
    user_name = note.get('user_name')
    create_time = note.get('create_time')
    title = note.get('title')
    desc = note.get('desc')
    image_id_list = note.get('image_id_list')
    video_url = note.get('video_url')

    current_date_path = f'download/{user_id}-{user_name}/{create_time}'
    os.makedirs(current_date_path, exist_ok=True)

    if title is not None:
        with open(current_date_path + '/title.txt', 'w', encoding='UTF-8') as f:
            f.write(title)
            f.flush()

    if desc is not None:
        with open(current_date_path + '/desc.txt', 'w', encoding='UTF-8') as f:
            f.write(desc)
            f.flush()

    if video_url is not None:
        video_name = video_url.split('/').pop().split('?')[0]
        video_file_path = current_date_path + f'/{video_name}'
        if os.path.isfile(video_file_path):
            print(f'{video_file_path} 已存在')
        else:
            print(f'{video_file_path} 下载中')
            res = requests.get(video_url, verify=False)
            if res.status_code != 200:
                print(f'视频请求失败，image_id：{video_url}')
                return
            content = res.content
            if not content:
                print(f'找不到视频数据，image_id：{video_url}')
                return
            with open(video_file_path, 'wb') as f:
                f.write(content)
            print(f'{video_file_path} 下载完毕')

    image_info_list = []
    for image_id in image_id_list:
        image_file_path = current_date_path + f'/{image_id}.png'
        if os.path.isfile(image_file_path):
            print(f'{image_file_path} 已存在')
            continue
        image_info_list.append({
            'image_id': image_id,
            'image_file_path': image_file_path
        })
    # 并发下载
    asyncio.run(async_download_image_main(image_info_list))


async def async_download_image_main(image_info_list: list):
    sem = asyncio.Semaphore(semaphore_of_download)
    async with aiohttp.ClientSession(headers=get_headers()) as client:
        tasks = [download_image_with_aiohttp(sem, item, client) for item in image_info_list]
        return await asyncio.gather(*tasks)


async def download_image_with_aiohttp(sem: asyncio.Semaphore, image_info_item: dict, client: aiohttp.ClientSession):
    image_id = image_info_item['image_id']
    image_file_path = image_info_item['image_file_path']
    async with sem:
        print(f'{image_file_path} 并发下载中')
        async with client.get(f'https://ci.xiaohongshu.com/{image_id}?imageView2/2/w/0/format/png', verify_ssl=False) as response:
            if response.status == 200:
                content = await response.content.read()
                await asyncio.sleep(0)
                if content:
                    with open(image_file_path, 'wb') as f:
                        f.write(content)
                print(f'{image_file_path} 下载完毕')
            else:
                print(f'{image_file_path} 下载异常❌')


def download_image(image_id: str, image_file_path: str):
    print(f'{image_file_path} 下载中')
    res = requests.get(f'https://ci.xiaohongshu.com/{image_id}?imageView2/2/w/0/format/png', verify=False)
    if res.status_code != 200:
        print(f'图片请求失败，image_id：{image_id}')
        return
    content = res.content
    if not content:
        print(f'找不到图片数据，image_id：{image_id}')
        return
    with open(image_file_path, 'wb') as f:
        f.write(content)
    print(f'{image_file_path} 下载完毕')


def download_all_note(note_id_list: list):
    parsed_note_info_list = get_parsed_note_info_list(note_id_list)
    for note in parsed_note_info_list:
        download_note(note)


def main():
    try:
        print('正在获取笔记')
        open_browser_and_login()
        print('获取首页笔记...')
        first_page_result = find_result_in_first_page()
        print('获取首页笔记成功')
        notes = first_page_result['user']['notes'][0]
        note_id_list = [{
            'noteId': item['noteCard']['noteId'],
            'xsecToken': item['noteCard']['xsecToken'],
            'displayTitle': item['noteCard']['displayTitle']
        } for item in notes]
        cursor = first_page_result['user']['noteQueries'][0]['cursor']
        note_id_list = recursion_scroll_until_no_more(note_id_list, cursor)
        # 翻转
        note_id_list = note_id_list[::-1]
        print(f'已获取笔记数量：{len(note_id_list)}')
        download_all_note(note_id_list)
        print('全部笔记下载完毕！')
    finally:
        browser.quit()


if __name__ == '__main__':
    main()
