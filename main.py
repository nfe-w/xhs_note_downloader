import json
import os
import re
import time
from datetime import datetime

import requests
import urllib3
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common import WebDriverException
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# PC web 端，用户id
profile_id = '5db5cecb00000000010008dc'

options = Options()
caps = {
    "browserName": "chrome",
    'goog:loggingPrefs': {'performance': 'ALL'}  # 开启日志性能监听
}
for key, value in caps.items():
    options.set_capability(key, value)
options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
browser = webdriver.Chrome(options=options)
browser.implicitly_wait(10)


def open_browser_and_login():
    browser.get(f'https://www.xiaohongshu.com/user/profile/{profile_id}')
    time.sleep(5)


def find_result_in_first_page() -> dict:
    page_content = browser.page_source
    soup = BeautifulSoup(page_content, "html.parser")
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


# 递归次数
recursion_count = 0


def recursion_scroll_until_no_more(note_id_list, cursor):
    if cursor is None and cursor != '':
        return
    global recursion_count
    recursion_count = recursion_count + 1
    print(f'正在进行第{recursion_count}次递归获取')
    # 向下滚动页面4次
    for _ in range(4):
        scroll_one_screen()
        time.sleep(1)
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
                'displayTitle': item['display_title']
            } for item in recursion_data['notes']]
            last_recursion_data = recursion_data
        except WebDriverException as e:
            print(e.msg)
            continue
    if last_recursion_data is not None and last_recursion_data['cursor'] is not None and last_recursion_data['cursor'] != '':
        recursion_scroll_until_no_more(note_id_list, last_recursion_data['cursor'])
    return note_id_list


def parse_note_by_note_id(parsed_note_info_list, note_id, note_index=0):
    urllib3.disable_warnings()
    response = requests.get(f'https://www.xiaohongshu.com/explore/{note_id}', verify=False)
    if response.status_code == 200:
        html_text = response.text
        soup = BeautifulSoup(html_text, "html.parser")
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
            return
        note = result['note']['noteDetailMap'][result['note']['firstNoteId']]['note']
        create_time = datetime.fromtimestamp(note['time'] / 1000).strftime("%Y-%m-%d %H%M%S")
        user_name = note['user']['nickname']
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
            'create_time': create_time,
            'user_name': user_name,
            'title': title,
            'desc': desc,
            'image_id_list': image_id_list,
            'video_url': video_url,
        })


def get_parsed_note_info_list(note_id_list):
    # 根据第一篇笔记获取用户名
    temp_list = []
    parse_note_by_note_id(temp_list, note_id_list[0]['noteId'])
    user_name = temp_list[0]['user_name']
    # 获取全部笔记信息
    parsed_note_info_list = []
    result_file_path = f'download/{user_name}/result.json'
    if os.path.isfile(result_file_path):
        print(f'{result_file_path}已存在，直接使用')
        with open(result_file_path, 'r') as f:
            parsed_note_info_list = json.loads(f.read())
    else:
        print(f'{result_file_path}不存在，获取并保存')
        note_count = 0
        for item in note_id_list:
            note_count = note_count + 1
            print(f'正在解析第{note_count}篇笔记')
            parse_note_by_note_id(parsed_note_info_list, item['noteId'], note_count)
        with open(result_file_path, 'w', encoding='UTF-8') as f:
            f.write(json.dumps(parsed_note_info_list, ensure_ascii=False))
            f.flush()
        print('获取全部json完毕')
    return parsed_note_info_list


def download_note(note):
    create_time = note.get('create_time')
    user_name = note.get('user_name')
    title = note.get('title')
    desc = note.get('desc')
    image_id_list = note.get('image_id_list')
    video_url = note.get('video_url')

    current_date_path = f'download/{user_name}/{create_time}'
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
        video_name = video_url.split('/').pop()
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

    for image_id in image_id_list:
        image_file_path = current_date_path + f'/{image_id}.png'
        if os.path.isfile(image_file_path):
            print(f'{image_file_path} 已存在')
            continue
        print(f'{image_file_path} 下载中')
        res = requests.get(f'https://ci.xiaohongshu.com/{image_id}?imageView2/2/w/0/format/png', verify=False)
        if res.status_code != 200:
            print(f'图片请求失败，image_id：{image_id}')
            continue
        content = res.content
        if not content:
            print(f'找不到图片数据，image_id：{image_id}')
            continue
        with open(image_file_path, 'wb') as f:
            f.write(content)
        print(f'{image_file_path} 下载完毕')


def download_all_note(note_id_list):
    parsed_note_info_list = get_parsed_note_info_list(note_id_list)
    for note in parsed_note_info_list:
        download_note(note)


def main():
    try:
        print('正在获取笔记')
        open_browser_and_login()
        first_page_result = find_result_in_first_page()
        notes = first_page_result['user']['notes'][0]
        note_id_list = [{
            'noteId': item['noteCard']['noteId'],
            'displayTitle': item['noteCard']['displayTitle']
        } for item in notes]
        cursor = first_page_result['user']['noteQueries'][0]['cursor']
        note_id_list = recursion_scroll_until_no_more(note_id_list, cursor)[::-1]
        print(f'已获取笔记数量：{len(note_id_list)}')
        download_all_note(note_id_list)
        print('全部笔记下载完毕！')
    finally:
        browser.quit()


if __name__ == '__main__':
    main()
