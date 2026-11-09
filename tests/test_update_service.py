import hashlib,io,threading,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import update_service as u

REPO='example/service-impact';BODY=b'verified release data'
def release(version=None):
    if version is None:version=str(u.version_tuple(u.app_meta.VERSION)[0]+1)+'.0.0'
    name='ServiceImpact-'+version+'-windows-x64.zip'
    return dict(tag_name='v'+version,draft=False,prerelease=False,body='Release notes',assets=[dict(name=name,state='uploaded',size=len(BODY),digest='sha256:'+hashlib.sha256(BODY).hexdigest(),
        browser_download_url='https://github.com/'+REPO+'/releases/download/v'+version+'/'+name,url='https://api.github.com/repos/'+REPO+'/releases/assets/123')])
class Opener:
    data=BODY
    def open(self,*a,**kw):return io.BytesIO(self.data)
class Tests(unittest.TestCase):
    def test_versions_use_numbers_and_skip_old(self):
        self.assertGreater(u.version_tuple('v2.10.0'),u.version_tuple('2.9.0'))
        self.assertFalse(u.parse_release(release('1.0.0'),REPO)['newer'])
    def test_stable_release_only(self):
        for key in ('draft','prerelease'):
            d=release();d[key]=True
            with self.assertRaises(ValueError):u.parse_release(d,REPO)
    def test_missing_digest_and_cross_repo_asset_rejected(self):
        d=release();d['assets'][0]['digest']=''
        with self.assertRaises(ValueError):u.parse_release(d,REPO)
        d=release();d['assets'][0]['browser_download_url']='https://github.com/other/project/releases/download/v2.1.0/test.zip'
        with self.assertRaises(ValueError):u.parse_release(d,REPO)
    def test_bad_versions_paths_and_redirects_rejected(self):
        for v in ('2.0','../../x','v2.1.0-beta','-1.0.0'):
            with self.assertRaises(ValueError):u.version_tuple(v)
        for url in ('http://github.com/file','https://evil.example/file','https://github.com.evil.example/file','https://user:secret@github.com/file'):
            with self.assertRaises(ValueError):u.safe_url(url)
    def test_verified_download_and_corruption_cleanup(self):
        with tempfile.TemporaryDirectory() as out:
            r=u.parse_release(release(),REPO);fake=Opener()
            with patch.object(u,'opener',return_value=fake):path=u.download(r,out,threading.Event())
            self.assertEqual(Path(path).read_bytes(),BODY)
            fake.data=b'wrong'
            with patch.object(u,'opener',return_value=fake),self.assertRaises(ValueError):u.download(r,out,threading.Event())
            self.assertEqual(Path(path).read_bytes(),BODY);self.assertFalse(list(Path(out).glob('*.part')))
    def test_cancel_does_not_create_available_package(self):
        cancel=threading.Event();cancel.set()
        with tempfile.TemporaryDirectory() as out,patch.object(u,'opener',return_value=Opener()):
            with self.assertRaises(InterruptedError):u.download(u.parse_release(release(),REPO),out,cancel)
            self.assertEqual(list(Path(out).iterdir()),[])
    def test_no_token_on_download_redirect(self):
        req=u.urllib.request.Request('https://api.github.com/test',headers={'Authorization':'Bearer TEST'})
        new=u.Redirect().redirect_request(req,None,302,'',{},'https://release-assets.githubusercontent.com/test')
        self.assertIsNone(new.get_header('Authorization'))
    def test_large_and_duplicate_asset_rejected(self):
        d=release();d['assets'][0]['size']=u.MAX_PACKAGE+1
        with self.assertRaises(ValueError):u.parse_release(d,REPO)
        d=release();d['assets']*=2
        with self.assertRaises(ValueError):u.parse_release(d,REPO)

if __name__=='__main__':unittest.main()
