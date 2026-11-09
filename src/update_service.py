"""GitHub Release discovery and verified portable-package downloads.

Never executes downloaded files. User data lives outside the release directory.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import app_meta

MAX_PACKAGE=200*1024*1024
ALLOWED_HOSTS={'api.github.com','github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'}

def version_tuple(value):
    m=re.fullmatch(r'v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})',value or '')
    if not m:raise ValueError('版本号应为 v主版本.次版本.修订号，例如 v2.0.1')
    return tuple(map(int,m.groups()))

def repository(value):
    value=value.strip().removeprefix('https://github.com/').rstrip('/').removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}',value):
        raise ValueError('请填写 GitHub 仓库，格式为 用户名/仓库名')
    if value.split('/')[1] in ('.','..'):raise ValueError('GitHub 仓库名无效')
    return value

def safe_url(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme!='https' or p.hostname not in ALLOWED_HOSTS or p.username or p.password or p.port not in (None,443):
        raise ValueError('更新地址不属于允许的 GitHub HTTPS 下载服务')
    return url

class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        safe_url(newurl)
        new=super().redirect_request(req,fp,code,msg,headers,newurl)
        if new and urllib.parse.urlsplit(newurl).hostname!='api.github.com':new.remove_header('Authorization')
        return new

def opener():return urllib.request.build_opener(Redirect())

def fetch_json(url,token=''):
    headers={'User-Agent':'ServiceImpact/'+app_meta.VERSION,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10'}
    if token:headers['Authorization']='Bearer '+token
    try:
        with opener().open(urllib.request.Request(safe_url(url),headers=headers),timeout=12) as r:data=r.read(2*1024*1024+1)
    except urllib.error.HTTPError as exc:
        if exc.code==404:raise ValueError('尚无正式 Release，或仓库不存在/私有仓库未授权') from None
        if exc.code in (401,403):raise ValueError('GitHub 访问受限或达到请求上限，请稍后重试；私有仓库需有效的只读令牌') from None
        raise ValueError('GitHub 检查失败（HTTP %d）'%exc.code) from None
    except OSError:raise ValueError('无法连接 GitHub，请检查网络后重试') from None
    if len(data)>2*1024*1024:raise ValueError('更新信息超出大小限制')
    try:return json.loads(data)
    except (ValueError,UnicodeError):raise ValueError('GitHub 返回的更新信息无效') from None

def parse_release(data,repo,current=app_meta.VERSION):
    repo=repository(repo)
    if not isinstance(data,dict) or data.get('draft') or data.get('prerelease'):raise ValueError('只接受已发布的正式版本')
    tag=data.get('tag_name','');remote=version_tuple(tag);new='.'.join(map(str,remote))
    newer=remote>version_tuple(current)
    expected=app_meta.ASSET_TEMPLATE.format(version=new)
    matches=[a for a in data.get('assets',[]) if a.get('name')==expected and a.get('state')=='uploaded']
    asset=None
    if newer:
        if len(matches)!=1:raise ValueError('发现新版，但缺少唯一的 Windows 更新包：'+expected)
        a=matches[0];digest=a.get('digest','')
        if not isinstance(digest,str) or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}',digest):
            raise ValueError('新版更新包缺少 SHA256 校验信息，已停止下载')
        size=a.get('size')
        if type(size)!=int or not 0<size<=MAX_PACKAGE:raise ValueError('更新包大小无效或超过 200 MiB')
        expected_url='https://github.com/'+repo+'/releases/download/'+urllib.parse.quote(tag,safe='')+'/'+expected
        if a.get('browser_download_url')!=expected_url:raise ValueError('更新包地址与当前仓库及版本不一致')
        api_url=a.get('url','')
        if not re.fullmatch(re.escape('https://api.github.com/repos/'+repo+'/releases/assets/')+r'\d+',api_url):raise ValueError('GitHub 更新资源地址无效')
        asset=dict(name=expected,url=expected_url,api_url=api_url,size=size,sha256=digest[7:].lower())
    return dict(version=new,newer=newer,asset=asset,notes=str(data.get('body') or '本版本未提供更新说明')[:30000],
                url='https://github.com/'+repo+'/releases/tag/'+urllib.parse.quote(tag,safe=''),repo=repo)

def check(repo,current=app_meta.VERSION,token=''):
    repo=repository(repo)
    return parse_release(fetch_json('https://api.github.com/repos/'+repo+'/releases/latest',token),repo,current)

def download(release,directory,cancel,progress=lambda done,total:None,token=''):
    version_tuple(release.get('version'))
    a=release.get('asset')
    if not a or not release.get('newer'):raise ValueError('没有可下载的新版本')
    name=a['name']
    if name!=app_meta.ASSET_TEMPLATE.format(version=release['version']):raise ValueError('更新文件名无效')
    folder=Path(directory);folder.mkdir(parents=True,exist_ok=True)
    destination=folder/name;partial=folder/(name+'.part')
    digest=hashlib.sha256();done=0
    headers={'User-Agent':'ServiceImpact/'+app_meta.VERSION,'Accept':'application/octet-stream'}
    url=a['api_url'] if token else a['url']
    if token:headers['Authorization']='Bearer '+token
    try:
        if cancel.is_set():raise InterruptedError('已取消下载')
        with opener().open(urllib.request.Request(safe_url(url),headers=headers),timeout=15) as r,partial.open('wb') as f:
            while True:
                if cancel.is_set():raise InterruptedError('已取消下载')
                block=r.read(65536)
                if not block:break
                done+=len(block)
                if done>a['size'] or done>MAX_PACKAGE:raise ValueError('下载内容超过声明大小')
                digest.update(block);f.write(block);progress(done,a['size'])
        if done!=a['size'] or digest.hexdigest()!=a['sha256']:raise ValueError('更新包校验失败，未保存为可用更新包')
        partial.replace(destination)
        return str(destination)
    finally:
        if partial.exists():partial.unlink()
