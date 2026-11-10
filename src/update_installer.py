"""Verified, transactional replacement of the portable Windows executable.

Only ServiceImpact.exe is extracted. Settings/results and other files are untouched.
The old executable is retained beside the target as a uniquely named backup.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
import update_service

MAX_EXE=200*1024*1024

def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def write(path,value):
    path=Path(path);tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8');tmp.replace(path)

def prepare(package,release,target,root,pid=None):
    target=Path(target).resolve();root=Path(root).resolve();package=Path(package).resolve()
    update_service.version_tuple(release['version'])
    asset=release.get('asset') or {}
    if package.stat().st_size!=asset.get('size') or sha(package)!=asset.get('sha256'):raise ValueError('更新包复核失败，原程序未改动')
    if target.suffix.lower()!='.exe' or not target.is_file() or target.is_symlink():raise ValueError('当前程序路径无效')
    # A write probe fails before the application exits, e.g. a read-only directory.
    with tempfile.TemporaryFile(dir=target.parent):pass
    root.mkdir(parents=True,exist_ok=True);stage=Path(tempfile.mkdtemp(prefix='install-',dir=root))
    exe=stage/'ServiceImpact.exe'
    try:
        with zipfile.ZipFile(package) as z:
            names=[]
            for entry in z.infolist():
                path=PurePosixPath(entry.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or ':' in entry.filename or stat.S_ISLNK(entry.external_attr>>16):
                    raise ValueError('更新包包含不安全路径')
                names.append(entry.filename)
            if len(names)!=len(set(names)) or names.count('ServiceImpact.exe')!=1:raise ValueError('更新包程序文件缺失或重复')
            item=z.getinfo('ServiceImpact.exe')
            if not 0<item.file_size<=MAX_EXE:raise ValueError('更新程序大小无效')
            with z.open(item) as source,exe.open('wb') as output:
                total=0
                while chunk:=source.read(65536):
                    total+=len(chunk)
                    if total>MAX_EXE:raise ValueError('更新程序超出大小限制')
                    output.write(chunk)
        with exe.open('rb') as stream:
            if stream.read(2)!=b'MZ':raise ValueError('更新包不是 Windows 程序')
        plan=dict(target=str(target),staged=str(exe),old_sha256=sha(target),new_sha256=sha(exe),
                  version=release['version'],pid=int(pid if pid is not None else os.getpid()),id=uuid.uuid4().hex)
        write(stage/'plan.json',plan)
        return stage/'plan.json'
    except BaseException:
        # Only the single known file we created is removed, never arbitrary ZIP paths.
        exe.unlink(missing_ok=True)
        raise

def environment():return {**os.environ,'PYINSTALLER_RESET_ENVIRONMENT':'1'}

def launch_helper(plan):
    if not getattr(sys,'frozen',False):raise ValueError('源码运行不支持替换 Python；请使用 Windows 发行版')
    plan=Path(plan).resolve();helper=plan.parent/'UpdateHelper.exe'
    shutil.copy2(sys.executable,helper)
    # The trusted current executable is the updater, not code from the downloaded ZIP.
    subprocess.Popen([str(helper),'--apply-update',str(plan)],cwd=plan.parent,env=environment(),
                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

def wait_for_exit(pid,timeout=120):
    if os.name!='nt':raise OSError('原地更新仅支持 Windows')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x100000,False,pid)
    if not handle:
        if ctypes.get_last_error()==87:return
        raise OSError('无法等待旧程序退出')
    try:
        if kernel.WaitForSingleObject(handle,int(timeout*1000))!=0:raise TimeoutError('旧程序未退出，更新未覆盖原程序')
    finally:kernel.CloseHandle(handle)

def retry_replace(source,destination,timeout=15):
    deadline=time.monotonic()+timeout
    while True:
        try:os.replace(source,destination);return
        except PermissionError:
            if time.monotonic()>=deadline:raise
            time.sleep(.25)

def apply(plan_file,wait=wait_for_exit,restart=None):
    plan_file=Path(plan_file).resolve();plan=json.loads(plan_file.read_text(encoding='utf8'));stage=plan_file.parent
    target=Path(plan['target']);staged=Path(plan['staged'])
    if not target.is_absolute() or target.suffix.lower()!='.exe' or staged.resolve()!=stage/'ServiceImpact.exe':raise ValueError('更新计划路径无效')
    if target.resolve().parent==stage or target.is_symlink():raise ValueError('更新目标无效')
    if not isinstance(plan.get('id'),str) or len(plan['id'])!=32 or any(c not in '0123456789abcdef' for c in plan['id']):raise ValueError('更新计划标识无效')
    update_service.version_tuple(plan['version'])
    result=stage/'result.json';backup=target.with_name(target.name+'.previous-'+plan['id']);incoming=target.with_name(target.name+'.new-'+plan['id'])
    replaced=False;backed_up=False;created_incoming=False
    try:
        if sha(staged)!=plan['new_sha256'] or sha(target)!=plan['old_sha256']:raise ValueError('程序内容已变化，更新已停止')
        wait(plan['pid'])
        if backup.exists() or incoming.exists():raise ValueError('更新暂存或备份文件已存在，原程序未改动')
        if sha(target)!=plan['old_sha256']:raise ValueError('旧程序已被其他更新替换')
        with incoming.open('xb') as output,staged.open('rb') as source:
            created_incoming=True;shutil.copyfileobj(source,output)
        if sha(incoming)!=plan['new_sha256']:raise ValueError('更新程序复制校验失败')
        retry_replace(target,backup);backed_up=True
        retry_replace(incoming,target);replaced=True
        write(result,dict(state='installed',version=plan['version'],backup=str(backup),target=str(target)))
        if restart is None:
            restart=lambda path:subprocess.Popen([str(path)],cwd=path.parent,env=environment())
        restart(target)
        write(result,dict(state='restarted',version=plan['version'],backup=str(backup),target=str(target)))
        return result
    except BaseException as exc:
        recovery='original_unchanged'
        if backed_up:
            try:
                # Restore only our own replaced executable; never overwrite an intervening change.
                if not target.exists() or (replaced and sha(target)==plan['new_sha256']):retry_replace(backup,target);recovery='rolled_back'
                else:recovery='backup_retained'
            except OSError:recovery='backup_retained'
        write(result,dict(state='failed',error=str(exc),recovery=recovery,backup=str(backup),target=str(target)))
        raise
    finally:
        if created_incoming:incoming.unlink(missing_ok=True)

def main(plan):
    try:apply(plan);return 0
    except Exception as exc:
        import tkinter as tk
        from tkinter import messagebox
        root=tk.Tk();root.withdraw()
        messagebox.showerror('更新未完成',str(exc)+'\n旧程序或备份已保留。详细记录：\n'+str(Path(plan).parent/'result.json'),parent=root)
        root.destroy();return 1
