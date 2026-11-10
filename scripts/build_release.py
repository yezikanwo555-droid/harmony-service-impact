"""Build a portable Windows release from an explicit, data-free allowlist."""
import hashlib
import shutil
from pathlib import Path
import runpy
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]

def main():
    if sys.platform!='win32':raise SystemExit('Build the Windows package on Windows.')
    meta=runpy.run_path(str(ROOT/'src/app_meta.py'))
    dist=ROOT/'dist';dist.mkdir(exist_ok=True)
    binary_dir=dist/('v'+meta['VERSION'])
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed',
                    '--name','ServiceImpact','--distpath',str(binary_dir),'--workpath',str(ROOT/'build'),
                    '--specpath',str(ROOT/'build'),'--paths',str(ROOT/'src'),str(ROOT/'src/impact_app.py')],check=True,cwd=ROOT)
    package=dist/meta['ASSET_TEMPLATE'].format(version=meta['VERSION'])
    with zipfile.ZipFile(package,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        z.write(binary_dir/'ServiceImpact.exe','ServiceImpact.exe')
        for name in ('README.md','CHANGELOG.md','docs/使用说明.txt','docs/网络环境说明.txt'):
            z.write(ROOT/name,name)
    digest=hashlib.sha256(package.read_bytes()).hexdigest()
    standalone=dist/('ServiceImpact-'+meta['VERSION']+'-windows-x64.exe')
    shutil.copy2(binary_dir/'ServiceImpact.exe',standalone)
    (dist/'SHA256SUMS.txt').write_text(digest+'  '+package.name+'\n'+hashlib.sha256(standalone.read_bytes()).hexdigest()+'  '+standalone.name+'\n',encoding='utf8')
    print(package)
    print('SHA256 '+digest)

if __name__=='__main__':main()
