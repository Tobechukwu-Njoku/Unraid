"""Offline adversarial checks; commands, sysfs, and raw devices are mocked.
Only temporary text evidence files are created. The supplied main is never invoked.
"""
import ast, dataclasses, errno, hashlib, io, json, os, pathlib, subprocess, tempfile, types
SOURCE=pathlib.Path(__file__).resolve().parent.parent/'sata_reallocator.py'
code=SOURCE.read_text()
compiled=compile(ast.parse(code),'attached-v8','exec')
def namespace():
 ns={'__name__':'review_only'};exec(compiled,ns);return ns
def drive(ns,**changes):
 d=dict(input_path='MOCK_DEVICE',real_path='MOCK_DEVICE',persistent_path='MOCK_DEVICE',sysfs_name='MOCK',dev_major=8,dev_minor=16,model='TEST',serial='TEST',sanitized_serial='TEST',logical_bs=512,physical_bs=512,alignment_offset=0,size_bytes=8192,orig_kernel_timeout='30',orig_sct_erc=(100,100));d.update(changes);return ns['SataDriveInfo'](**d)
out={'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'syntax':'PASS','cases':[]}
def add(name,result):out['cases'].append({'case':name,'observed':result})
def journal(ns,root,d,name):
 p=root/(name+'.jsonl');df=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
 j=ns['WriteAheadJournal'](p,df,d,'OLD',set());return j,df
# Confirm repaired identity-gated cleanup and current VERIFY classification.
for mode in ['command_failure','timeout','different_serial','missing_path']:
 ns=namespace();d=drive(ns);actions=[]
 class FakeTimeoutPath:
  def exists(self):return True
  def write_text(self,value,**kw):actions.append(('timeout_write',value.strip()))
  def read_text(self,**kw):return '30'
 ns['pathlib']=types.SimpleNamespace(Path=lambda *a:FakeTimeoutPath())
 ns['os']=types.SimpleNamespace(path=types.SimpleNamespace(exists=lambda *a:mode!='missing_path'))
 ns['get_block_rdev']=lambda *a:(8,16)
 def cmd(c,**kw):
  actions.append(c)
  if '-i' in c:
   if mode=='timeout':raise subprocess.TimeoutExpired('mock',1)
   return types.SimpleNamespace(returncode=4 if mode=='command_failure' else 0,stdout=json.dumps({'model_name':'OTHER','serial_number':'OTHER'}),stderr='')
  return types.SimpleNamespace(returncode=0,stdout='',stderr='')
 ns['run_cmd']=cmd;ns['query_current_sct_erc']=lambda *a:(100,100)
 errors=ns['restore_recovery_settings'](d,True,True)
 mutations=[a for a in actions if a[0]=='timeout_write' or (a[0]=='smartctl' and '-l' in a)]
 assert not mutations and errors
 add('cleanup '+mode,{'mutation_count':len(mutations),'errors':errors})
for title,rc,text,expected in [
 ('medium error',18,'Sense Key: Medium Error\nVERIFY(16) medium or hardware error, reported lba=0x80\n','DEFECT'),
 ('out of range',18,'Sense Key: Medium Error\nreported lba=0x9999\n','INDETERMINATE'),
 ('unsupported opcode',9,'Sense Key: Illegal Request\nAdditional sense: Invalid command operation code\n','UNSUPPORTED'),
 ('invalid field',5,'Sense Key: Illegal Request\nAdditional sense: Invalid field in cdb\n','INDETERMINATE')]:
 ns=namespace();ns['run_cmd']=lambda *a,rc=rc,text=text,**kw:types.SimpleNamespace(returncode=rc,stdout='',stderr=text)
 r=ns['run_ata_media_verify'](drive(ns),128,1);assert r.category==expected;add('VERIFY '+title,dataclasses.asdict(r))
for label,result in [('short_write',128),('non_EIO_error',None)]:
 ns=namespace();events=[]
 class MemoryJournal:
  run_id='RUN'
  def append(self,event,payload):events.append({'event_type':event,**payload})
 def write(*a):
  if result is None:raise OSError(errno.EINVAL,'mock invalid direct IO')
  return result
 ns['os']=types.SimpleNamespace(pwritev=write,RWF_DSYNC=2)
 r=ns['overwrite_and_verify_physical_sector'](0,drive(ns),0,8192,io.BytesIO(bytes(512)),io.BytesIO(bytes(512)),journal=MemoryJournal())
 assert [e['event_type'] for e in events]==['WRITE_INTENT','ATTEMPT_OUTCOME']
 add('writer '+label,{'status':r['status'],'events':events})
with tempfile.TemporaryDirectory(prefix='reallocator-v8-text-review-') as tmp:
 root=pathlib.Path(tmp);os.chmod(root,0o700)
 ns=namespace();shared=root/'shared';shared.mkdir(mode=0o777);os.chmod(shared,0o777)
 try:ns['prepare_private_evidence_dir'](shared,min_free_bytes=0);raise AssertionError('accepted')
 except ns['ReallocatorError'] as e:add('shared directory rejected',{'error':str(e),'mode_after':oct(shared.stat().st_mode&0o777)})
 assert (shared.stat().st_mode&0o777)==0o777
 for fd in [None,99999999]:
  try:ns['open_safe_regular_file'](root/'unused',os.O_RDONLY,set(),dir_fd=fd);raise AssertionError('accepted')
  except ns['ReallocatorError'] as e:add('invalid dirfd '+str(fd),str(e))
 # A real durable journal, operation reconciliation, then normal startup. Hardware interfaces mocked.
 ns=namespace();d=drive(ns);j,df=journal(ns,root,d,'pending');reads=[]
 try:
  j.append('RUN_START',{'mode':'EXECUTE'})
  j.append('WRITE_INTENT',{'offset_bytes':0,'attempt':1,'operation':'pwritev_zero','op_id':'OLD:0:1:pwritev_zero'})
  j.run_id='RECOVERY';ns['assert_device_identity_and_capacity']=lambda *a:None
  ns['restore_recovery_settings']=lambda *a,**k:[]
  ns['confirm_live_defect']=lambda *a:(reads.append(a[2]) or ('READABLE','mock readable'))
  ns['reconcile_interrupted_journal_state'](d,0,j);j.append('RUN_CLOSURE',{'exit_code':0})
  try:j.reconcile_prior_records();restart='ACCEPTED'
  except ns['ReallocatorError'] as e:restart=str(e)
  assert restart=='ACCEPTED'
  add('pending write recovery then restart',{'recovery_returned_success':True,'inspected_offsets':reads,'restart':restart,'journal':pathlib.Path(j.path).read_text().splitlines()})
 finally:j.close();os.close(df)
 # Baseline selection and unnecessary inspection after a fully successful earlier run.
 ns=namespace();d=drive(ns,orig_kernel_timeout='60',orig_sct_erc=(70,70));j,df=journal(ns,root,d,'baselines');restores=[];reads=[]
 try:
  j.append('RUN_START',{'mode':'EXECUTE'})
  j.append('SETTING_MUTATION_INTENT',{'setting':'kernel_timeout','original':'30','target':'60'})
  j.append('SETTING_RESTORED',{'setting':'kernel_timeout','restored_value':'30'})
  j.append('SETTING_MUTATION_INTENT',{'setting':'sct_erc','original':[100,100],'target':[70,70]})
  j.append('SETTING_RESTORED',{'setting':'sct_erc','restored_value':[100,100]})
  j.append('WRITE_INTENT',{'offset_bytes':0,'attempt':1})
  j.append('ATTEMPT_OUTCOME',{'offset_bytes':0,'attempt':1,'status':'VERIFIED_LOGICAL_READBACK_AND_MEDIA_VERIFY'})
  j.append('RUN_CLOSURE',{'exit_code':0})
  # User changed settings between runs; this run was interrupted after changing them.
  j.run_id='LATER';j.append('RUN_START',{'mode':'EXECUTE'})
  j.append('SETTING_MUTATION_INTENT',{'setting':'kernel_timeout','original':'90','target':'60'})
  j.append('SETTING_MUTATION_INTENT',{'setting':'sct_erc','original':[200,200],'target':[70,70]})
  j.run_id='RECOVERY';ns['assert_device_identity_and_capacity']=lambda *a:None
  def restore(baseline,*a,**kw):
   restores.append({'timeout':baseline.orig_kernel_timeout,'erc':baseline.orig_sct_erc})
   for rid in kw.get('timeout_target_run_ids',[]):kw['journal'].append('SETTING_RESTORED',{'target_run_id':rid,'setting':'kernel_timeout','restored_value':baseline.orig_kernel_timeout})
   for rid in kw.get('erc_target_run_ids',[]):kw['journal'].append('SETTING_RESTORED',{'target_run_id':rid,'setting':'sct_erc','restored_value':list(baseline.orig_sct_erc)})
   return []
  ns['restore_recovery_settings']=restore
  ns['confirm_live_defect']=lambda *a:(reads.append(a[2]) or ('READABLE','mock readable'))
  ns['reconcile_interrupted_journal_state'](d,0,j);j.append('RUN_CLOSURE',{'exit_code':0});j.reconcile_prior_records()
  assert restores==[{'timeout':'90','erc':(200,200)}] and reads==[]
  add('outstanding baseline ignores closed historical baseline',{'expected_timeout':'90','expected_erc':[200,200],'actual_restoration_arguments':restores,'already_resolved_offsets_reinspected':reads,'restart':'ACCEPTED'})
 finally:j.close();os.close(df)
 # An indeterminate confirmation must be inspected and remain unresolved.
 ns=namespace();d=drive(ns);j,df=journal(ns,root,d,'confirmation');reads=[]
 try:
  j.append('RUN_START',{});j.append('CONFIRMATION_INDETERMINATE_STOP',{'offset_bytes':512,'reason':'mock transport error'});j.append('RUN_CLOSURE',{'exit_code':3})
  j.run_id='RECOVERY';ns['assert_device_identity_and_capacity']=lambda *a:None;ns['restore_recovery_settings']=lambda *a,**kw:[]
  ns['confirm_live_defect']=lambda *a:(reads.append(a[2]) or ('INDETERMINATE','still broken'))
  try:ns['reconcile_interrupted_journal_state'](d,0,j);raise AssertionError('indeterminate accepted')
  except ns['ReallocatorError'] as e:recovery_error=str(e)
  assert reads==[512]
  try:j.reconcile_prior_records();raise AssertionError('unresolved stop accepted')
  except ns['ReallocatorError'] as e:restart_error=str(e)
  add('confirmation stop remains blocked if inspection indeterminate',{'inspected_offsets':reads,'recovery_error':recovery_error,'restart_error':restart_error})
 finally:j.close();os.close(df)
 # Successful historical writes should not prevent a later pre-write scan from resuming.
 ns=namespace();d=drive(ns);j,df=journal(ns,root,d,'resume')
 try:
  j.append('RUN_START',{});j.append('WRITE_INTENT',{'offset_bytes':0,'attempt':1});j.append('ATTEMPT_OUTCOME',{'offset_bytes':0,'attempt':1,'status':'VERIFIED_LOGICAL_READBACK_AND_MEDIA_VERIFY'});j.append('RUN_CLOSURE',{'exit_code':0})
  j.run_id='DISCOVERY';j.append('RUN_START',{'mode':'DRY_RUN'});j.append('UNHANDLED_EXCEPTION',{'exception_type':'TimeoutExpired'});j.append('RUN_CLOSURE',{'exit_code':1})
  try:j.reconcile_prior_records(allow_discovery_resume=True);r='ACCEPTED'
  except ns['ReallocatorError'] as e:r=str(e)
  assert r=='ACCEPTED';add('discovery resume after old successful writes',r)
 finally:j.close();os.close(df)
 # New journal containing discovery only DOES resume.
 ns=namespace();d=drive(ns);j,df=journal(ns,root,d,'fresh-resume')
 try:
  j.append('RUN_START',{});j.append('UNHANDLED_EXCEPTION',{'exception_type':'TimeoutExpired'});j.append('RUN_CLOSURE',{'exit_code':1})
  j.run_id='NEW';j.reconcile_prior_records(allow_discovery_resume=True);add('discovery resume without historical writes','ACCEPTED')
 finally:j.close();os.close(df)
 # External ddrescue creation under a normal umask: emulate only fopen map text behavior.
 ns=namespace();d=drive(ns);mp=root/'scan.map';meta=root/'scan.meta.json';df=os.open(root,os.O_RDONLY|os.O_DIRECTORY);calls=[]
 ns['verify_dev_null_sink']=lambda:'MOCK_NULL'
 def ddrescue(cmd,**kw):
  calls.append(cmd)
  if '--version' not in cmd:
   with open(mp,'w') as f:f.write('0x0 + 1\n0x0 0x2000 +\n')
  return types.SimpleNamespace(returncode=0,stdout='GNU ddrescue mock',stderr='')
 ns['run_cmd']=ddrescue;old_umask=os.umask(0o022)
 try:
  try:ns['run_ddrescue_and_parse'](d,mp,meta,df,set(),8192);r='ACCEPTED'
  except ns['ReallocatorError'] as e:r=str(e)
  assert r=='ACCEPTED' and (mp.stat().st_mode&0o777)==0o600
  add('ddrescue map created with umask 022',{'map_mode':oct(mp.stat().st_mode&0o777),'result':r,'scan_commands':calls})
 finally:os.umask(old_umask);os.close(df)
 # Metadata-only scan interruption is now recoverable with its resume flag.
 ns=namespace();d=drive(ns);mp=root/'pre.map';meta=root/'pre.meta.json';df=os.open(root,os.O_RDONLY|os.O_DIRECTORY);attempts=[]
 ns['verify_dev_null_sink']=lambda:'MOCK_NULL'
 def interrupted(cmd,**kw):
  if '--version' in cmd:return types.SimpleNamespace(returncode=0,stdout='GNU ddrescue mock',stderr='')
  attempts.append(cmd);raise subprocess.TimeoutExpired('mock before map checkpoint',1)
 ns['run_cmd']=interrupted
 try:
  for resume in [False,True]:
   try:ns['run_ddrescue_and_parse'](d,mp,meta,df,set(),8192,allow_resume_interrupted=resume)
   except subprocess.TimeoutExpired:pass
  assert len(attempts)==2;add('metadata-only interrupted scan restart',{'stage_a_attempts':len(attempts),'map_exists':mp.exists()})
 finally:os.close(df)
# Recovery must itself be restartable after every durable record.
with tempfile.TemporaryDirectory(prefix='reallocator-v8-recovery-check-') as tmp:
 root=pathlib.Path(tmp);os.chmod(root,0o700)
 for prior_restoration_error in [False,True]:
  ns=namespace();d=drive(ns,orig_kernel_timeout='60',orig_sct_erc=None);j,df=journal(ns,root,d,'recovery-crash-'+str(prior_restoration_error));settings={'timeout':'60','writes':[]}
  try:
   j.append('RUN_START',{'mode':'EXECUTE'})
   j.append('SETTING_MUTATION_INTENT',{'setting':'kernel_timeout','original':'30','target':'60'})
   if prior_restoration_error:j.append('UNRESOLVED_RESTORATION_STATE',{'errors':['mock earlier restoration failure']})
   j.append('RUN_CLOSURE',{'exit_code':4 if prior_restoration_error else 1})
   class FakeSysfsPath:
    def exists(self):return True
    def write_text(self,value,**kw):settings['timeout']=value.strip();settings['writes'].append(value.strip())
    def read_text(self,**kw):return settings['timeout']
   ns['pathlib']=types.SimpleNamespace(Path=lambda *a:FakeSysfsPath())
   ns['assert_device_identity_and_capacity']=lambda *a:None
   ns['verify_cleanup_target_identity']=lambda *a:None
   ns['run_cmd']=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('unexpected external command'))
   original_append=j.append
   def interrupted_append(event,payload):
    if event==('RESTORATION_STATE_RECONCILED' if prior_restoration_error else 'STATE_RECONCILED'):
     raise RuntimeError('simulated interruption after durable SETTING_RESTORED')
    return original_append(event,payload)
   j.run_id='RECOVERY_1';j.append=interrupted_append
   try:ns['reconcile_interrupted_journal_state'](d,0,j);raise AssertionError('fault not reached')
   except RuntimeError:pass
   j.append=original_append;j.append('UNHANDLED_EXCEPTION',{'exception_type':'RuntimeError'});j.append('RUN_CLOSURE',{'exit_code':1})
   intermediate=ns['replay_journal_state'](ns['read_all_bytes_from_fd'](j.fd),d,j.path)
   j.run_id='RECOVERY_2'
   try:
    ns['reconcile_interrupted_journal_state'](d,0,j);j.append('RUN_CLOSURE',{'exit_code':0});j.reconcile_prior_records();result='ACCEPTED'
   except ns['ReallocatorError'] as e:result=str(e)
   assert result=='ACCEPTED'
   add('recovery interrupted after settings restored; prior error='+str(prior_restoration_error),{'settings':settings,'pending_setting_mutations_after_interruption':len(intermediate.pending_setting_mutations),'unresolved_restoration_events_after_interruption':len(intermediate.unresolved_restoration_events),'second_recovery_result':result,'journal_records':pathlib.Path(j.path).read_text().splitlines()})
  finally:j.close();os.close(df)
# Verify command wrapper's child mask and restoration on both completion and error.
for fail in [False,True]:
 ns=namespace();observed={};old_mask=os.umask(0o022)
 def fake_subprocess(cmd,**kw):
  mask=os.umask(0o022);os.umask(mask);observed.update(child_umask=kw['umask'],parent_mask_during_call=mask)
  if fail:raise RuntimeError('mock process error')
  return types.SimpleNamespace(returncode=0,stdout='',stderr='')
 ns['subprocess']=types.SimpleNamespace(run=fake_subprocess,PIPE=-1)
 try:
  try:ns['run_cmd'](['MOCK_COMMAND'])
  except RuntimeError:
   assert fail
  after=os.umask(0o022);os.umask(after);assert after==0o022 and observed=={'child_umask':0o077,'parent_mask_during_call':0o077}
  add('run_cmd umask fail='+str(fail),{**observed,'parent_mask_after_call':after})
 finally:os.umask(old_mask)
with tempfile.TemporaryDirectory(prefix='reallocator-v8-extra-text-') as tmp:
 root=pathlib.Path(tmp);os.chmod(root,0o700)
 ns=namespace();d=drive(ns);j,df=journal(ns,root,d,'confirmation-readable');reads=[]
 try:
  j.append('RUN_START',{});j.append('CONFIRMATION_INDETERMINATE_STOP',{'offset_bytes':512});j.append('RUN_CLOSURE',{'exit_code':3})
  j.run_id='RECOVERY';ns['assert_device_identity_and_capacity']=lambda *a:None
  ns['confirm_live_defect']=lambda *a:(reads.append(a[2]) or ('READABLE','mock readable'))
  ns['reconcile_interrupted_journal_state'](d,0,j);j.append('RUN_CLOSURE',{'exit_code':0});j.reconcile_prior_records()
  assert reads==[512];add('confirmation stop retired after readable inspection',{'inspected_offsets':reads,'restart':'ACCEPTED'})
 finally:j.close();os.close(df)
 ns=namespace();d=drive(ns);j,df=journal(ns,root,d,'contradictory');calls=[]
 try:
  j.append('RUN_START',{});j.append('SETTING_MUTATION_INTENT',{'setting':'kernel_timeout','original':'30'})
  j.run_id='OTHER';j.append('RUN_START',{});j.append('SETTING_MUTATION_INTENT',{'setting':'kernel_timeout','original':'90'})
  j.run_id='RECOVERY';ns['assert_device_identity_and_capacity']=lambda *a:None
  ns['restore_recovery_settings']=lambda *a,**kw:(calls.append(a) or [])
  try:ns['reconcile_interrupted_journal_state'](d,0,j);raise AssertionError('contradiction accepted')
  except ns['ReallocatorError'] as e:error=str(e)
  assert 'Contradictory unresolved' in error and not calls
  add('contradictory outstanding baselines refused before restoration',{'error':error,'restoration_calls':len(calls)})
 finally:j.close();os.close(df)
 for category,degraded,expected in [('OK',False,'VERIFIED_LOGICAL_READBACK_AND_MEDIA_VERIFY'),('UNSUPPORTED',True,'DEGRADED_LOGICAL_READBACK_ONLY_NO_MEDIA_VERIFY'),('UNSUPPORTED',False,'VERIFY_UNSUPPORTED_REJECTED(rc=9)')]:
  ns=namespace();d=drive(ns);j,df=journal(ns,root,d,'write-'+category+'-'+str(degraded))
  try:
   proxy=types.SimpleNamespace(**vars(os));proxy.pwritev=lambda *a:512
   def preadv(fd,buffers,offset):buffers[0].seek(0);buffers[0].write(bytes(512));return 512
   proxy.preadv=preadv;ns['os']=proxy
   ns['flush_drive_caches']=lambda *a:(True,'mock cache flush')
   ns['run_ata_media_verify']=lambda *a:ns['VerifyResult'](category,0 if category=='OK' else 9,None,None,None,'mock verify')
   j.append('RUN_START',{'mode':'EXECUTE'})
   result=ns['overwrite_and_verify_physical_sector'](0,d,0,8192,io.BytesIO(bytes(512)),io.BytesIO(bytes(512)),journal=j,allow_degraded_no_verify=degraded)
   assert result['status']==expected
   j.append('RUN_CLOSURE',{'exit_code':0 if category=='OK' or degraded else 2})
   replay=ns['replay_journal_state'](ns['read_all_bytes_from_fd'](j.fd),d,j.path)
   assert not replay.pending_intents
   assert bool(replay.unresolved_outcomes)==(category=='UNSUPPORTED' and not degraded)
   add('post-write '+category+' degraded='+str(degraded),{'status':result['status'],'pending_intents':len(replay.pending_intents),'unresolved_outcomes':len(replay.unresolved_outcomes)})
  finally:j.close();os.close(df)
# Replay every durable recovery prefix with timeout + ERC + three target obligation types.
# The saved prefixes are ordinary text journal fixtures, never media copies.
with tempfile.TemporaryDirectory(prefix='reallocator-v8-prefix-text-') as tmp:
 root=pathlib.Path(tmp);os.chmod(root,0o700)
 def wire_recovery(ns,settings):
  class SysfsTimeout:
   def exists(self):return True
   def write_text(self,value,**kw):settings['timeout']=value.strip();settings['writes'].append(['timeout',value.strip()])
   def read_text(self,**kw):return settings['timeout']
  ns['pathlib']=types.SimpleNamespace(Path=lambda *a:SysfsTimeout())
  ns['assert_device_identity_and_capacity']=lambda *a:None
  ns['verify_cleanup_target_identity']=lambda *a:None
  ns['query_current_sct_erc']=lambda *a:tuple(settings['erc'])
  def command(cmd,**kw):
   spec=cmd[2]
   assert cmd[:2]==['smartctl','-l'] and spec.startswith('scterc,')
   settings['erc']=list(map(int,spec.split(',')[1:]));settings['writes'].append(['erc',settings['erc'][:]])
   return types.SimpleNamespace(returncode=0,stdout='',stderr='')
  ns['run_cmd']=command
  ns['confirm_live_defect']=lambda *a:('READABLE','mock readable')
 ns=namespace();d=drive(ns,orig_kernel_timeout='60',orig_sct_erc=(70,70));j,df=journal(ns,root,d,'prefix-template');states=[]
 settings={'timeout':'60','erc':[70,70],'writes':[]};wire_recovery(ns,settings)
 try:
  j.append('RUN_START',{'mode':'EXECUTE'})
  j.append('SETTING_MUTATION_INTENT',{'setting':'kernel_timeout','original':'30','target':'60'})
  j.append('SETTING_MUTATION_INTENT',{'setting':'sct_erc','original':[100,100],'target':[70,70]})
  j.append('WRITE_INTENT',{'offset_bytes':0,'attempt':1})
  j.append('WRITE_INTENT',{'offset_bytes':512,'attempt':1})
  j.append('ATTEMPT_OUTCOME',{'offset_bytes':512,'attempt':1,'status':'WRITE_EIO_FAILED'})
  j.append('CONFIRMATION_INDETERMINATE_STOP',{'offset_bytes':1024})
  j.append('UNRESOLVED_RESTORATION_STATE',{'errors':['mock timeout and ERC restoration failure']})
  j.append('RUN_CLOSURE',{'exit_code':4})
  seed_lines=pathlib.Path(j.path).read_text().splitlines(keepends=True)
  original_append=j.append
  def observe(event,payload):
   original_append(event,payload)
   states.append({'event':event,'lines':pathlib.Path(j.path).read_text().splitlines(keepends=True),'timeout':settings['timeout'],'erc':settings['erc'][:]})
  j.run_id='RECOVERY_FIRST';j.append=observe
  ns['reconcile_interrupted_journal_state'](d,0,j);j.append('RUN_CLOSURE',{'exit_code':0})
 finally:j.close();os.close(df)
 prefixes=[{'event':'BEFORE_RECOVERY','lines':seed_lines,'timeout':'60','erc':[70,70]}]+states
 for index,prefix in enumerate(prefixes):
  ns=namespace();d=drive(ns,orig_kernel_timeout=prefix['timeout'],orig_sct_erc=tuple(prefix['erc']))
  jp=root/('prefix-'+str(index)+'.jsonl');jp.write_text(''.join(prefix['lines']));os.chmod(jp,0o600)
  df=os.open(root,os.O_RDONLY|os.O_DIRECTORY);j=ns['WriteAheadJournal'](jp,df,d,'RECOVERY_NEXT',set())
  settings={'timeout':prefix['timeout'],'erc':prefix['erc'][:],'writes':[]};wire_recovery(ns,settings)
  try:
   ns['reconcile_interrupted_journal_state'](d,0,j);j.append('RUN_CLOSURE',{'exit_code':0});j.reconcile_prior_records()
   assert settings['timeout']=='30' and settings['erc']==[100,100]
   add('restart after durable prefix '+str(index)+' '+prefix['event'],{'restart':'ACCEPTED','final_timeout':settings['timeout'],'final_erc':settings['erc'],'restoration_writes':settings['writes']})
  finally:j.close();os.close(df)
 # Deliberately drift a setting in Phase B: verification must reject without rewriting it.
 prefix=next(p for p in prefixes if p['event']=='RESTORATION_STATE_RECONCILED')
 for issue in ['timeout_drift','erc_drift','identity_failure']:
  ns=namespace();d=drive(ns,orig_kernel_timeout=prefix['timeout'],orig_sct_erc=tuple(prefix['erc']))
  jp=root/(issue+'.jsonl');jp.write_text(''.join(prefix['lines']));os.chmod(jp,0o600)
  df=os.open(root,os.O_RDONLY|os.O_DIRECTORY);j=ns['WriteAheadJournal'](jp,df,d,'RECOVERY_VERIFY',set())
  settings={'timeout':prefix['timeout'],'erc':prefix['erc'][:],'writes':[]};wire_recovery(ns,settings)
  if issue=='timeout_drift':settings['timeout']='90'
  if issue=='erc_drift':settings['erc']=[200,200]
  if issue=='identity_failure':ns['verify_cleanup_target_identity']=lambda *a:'mock identity failure'
  try:
   try:ns['reconcile_interrupted_journal_state'](d,0,j);raise AssertionError('bad state accepted')
   except ns['ReallocatorError'] as e:error=str(e)
   assert 'Live settings verification failed' in error and not settings['writes']
   add('Phase B '+issue,{'error':error,'restoration_writes':settings['writes']})
  finally:j.close();os.close(df)

# Corroboration preserves the immediate outcome and durably records the final classification.
ns=namespace();d=drive(ns);events=[]
class MemoryOutcomeJournal:
 run_id='OUTCOME_TEST'
 def append(self,event,payload):events.append({'event_type':event,**payload})
def failed_write(*a):raise OSError(errno.EIO,'mock write EIO')
ns['os']=types.SimpleNamespace(pwritev=failed_write,RWF_DSYNC=2)
ns['run_ata_media_verify']=lambda *a:ns['VerifyResult']('INDETERMINATE',33,None,None,None,'mock transport failure')
r=ns['overwrite_and_verify_physical_sector'](0,d,0,8192,io.BytesIO(bytes(512)),io.BytesIO(bytes(512)),journal=MemoryOutcomeJournal())
recorded=next(e['status'] for e in events if e['event_type']=='ATTEMPT_OUTCOME')
assert recorded=='WRITE_EIO_FAILED' and r['status'].startswith('EIO_OR_DEFECT_UNCORROBORATED_INDETERMINATE')
assert recorded not in ns['RESOLVED_TARGET_STATUSES'] and r['status'] not in ns['RESOLVED_TARGET_STATUSES']
corroboration=next(e for e in events if e['event_type']=='RETRY_CORROBORATION_FAILED')
assert corroboration['status']==r['status'] and corroboration['prior_attempt_status']==recorded
add('corroboration classification recorded separately from immediate attempt',{'attempt_status':recorded,'corroboration_status':corroboration['status'],'returned_status':r['status'],'both_unresolved':True})

# Validate the new event against real journal replay, including operation mismatches.
with tempfile.TemporaryDirectory(prefix='reallocator-v8-corroboration-text-') as tmp:
 root=pathlib.Path(tmp);os.chmod(root,0o700);ns=namespace();d=drive(ns);j,df=journal(ns,root,d,'corroboration');events=[]
 try:
  proxy=types.SimpleNamespace(**vars(os))
  def eio(*a):raise OSError(errno.EIO,'mock write failure')
  proxy.pwritev=eio;ns['os']=proxy
  ns['run_ata_media_verify']=lambda *a:ns['VerifyResult']('INDETERMINATE',33,None,None,None,'mock timeout')
  j.append('RUN_START',{'mode':'EXECUTE'})
  result=ns['overwrite_and_verify_physical_sector'](0,d,0,8192,io.BytesIO(bytes(512)),io.BytesIO(bytes(512)),journal=j)
  raw=ns['read_all_bytes_from_fd'](j.fd);records=[json.loads(line) for line in raw.splitlines()]
  replay=ns['replay_journal_state'](raw,d,j.path)
  assert replay.unresolved_outcomes[0]['status']==result['status']
  assert replay.unresolved_outcomes[0]['corroboration_detail']=='mock timeout'
  assert replay.unresolved_outcomes[0]['prior_attempt_status']=='WRITE_EIO_FAILED'
  assert not replay.pending_intents
  add('V8 durable corroboration matches returned status',{'returned_status':result['status'],'replayed_outcome':replay.unresolved_outcomes[0]})
  event=records[-1];assert event['event_type']=='RETRY_CORROBORATION_FAILED'
  for field,value in [('run_id','OTHER'),('attempt',99),('operation','OTHER'),('op_id','OTHER'),('offset_bytes',512),('status',''),('status','VERIFIED_LOGICAL_READBACK_AND_MEDIA_VERIFY'),('status','DEGRADED_LOGICAL_READBACK_ONLY_NO_MEDIA_VERIFY')]:
   altered=[*records[:-1],{**event,field:value}]
   try:ns['replay_journal_state'](('\n'.join(json.dumps(r) for r in altered)+'\n').encode(),d,j.path);raise AssertionError('bad corroboration accepted')
   except ns['ReallocatorError'] as exc:add('corroboration rejects '+field+'='+str(value),str(exc))
  try:ns['replay_journal_state']((json.dumps(event)+'\n').encode(),d,j.path);raise AssertionError('orphan accepted')
  except ns['ReallocatorError'] as exc:add('orphan corroboration rejected',str(exc))
  # A crash before the extra record retains an unresolved immediate outcome.
  before=('\n'.join(json.dumps(r) for r in records[:-1])+'\n').encode()
  pre=ns['replay_journal_state'](before,d,j.path);assert pre.unresolved_outcomes[0]['status']=='WRITE_EIO_FAILED'
  add('interruption before corroboration remains unresolved',{'status':pre.unresolved_outcomes[0]['status']})
  j.run_id='RECOVERY';ns['assert_device_identity_and_capacity']=lambda *a:None
  ns['confirm_live_defect']=lambda *a:('READABLE','mock readable')
  ns['reconcile_interrupted_journal_state'](d,0,j);j.append('RUN_CLOSURE',{'exit_code':0});j.reconcile_prior_records()
  add('corroborated failure recovery then ordinary startup','ACCEPTED')
 finally:j.close();os.close(df)
# Inspect directory sync targets. This traces syscalls, not physical crash behavior.
with tempfile.TemporaryDirectory(prefix='reallocator-v8-directory-text-') as tmp:
 root=pathlib.Path(tmp);os.chmod(root,0o700);ns=namespace();synced=[]
 proxy=types.SimpleNamespace(**vars(os))
 def fsync_spy(fd):
  st=os.fstat(fd);synced.append((st.st_dev,st.st_ino));os.fsync(fd)
 proxy.fsync=fsync_spy;ns['os']=proxy
 child=root/'evidence';fd=ns['prepare_private_evidence_dir'](child,min_free_bytes=0)
 try:
  parent_stat=root.stat();child_stat=child.stat()
  parent_synced=(parent_stat.st_dev,parent_stat.st_ino) in synced
  child_synced=(child_stat.st_dev,child_stat.st_ino) in synced
  assert child_synced and not parent_synced
  add('new evidence directory parent durability',{'child_directory_fsynced':child_synced,'containing_directory_fsynced':parent_synced})
 finally:os.close(fd)
# Reproduce an append that writes a prefix and raises ENOSPC; do not fill any disk.
with tempfile.TemporaryDirectory(prefix='reallocator-v8-torn-journal-text-') as tmp:
 root=pathlib.Path(tmp);os.chmod(root,0o700)
 for append_after_error in [False,True]:
  ns=namespace();d=drive(ns);j,df=journal(ns,root,d,'torn-'+str(append_after_error));restores=[]
  try:
   j.append('RUN_START',{'mode':'EXECUTE'})
   j.append('SETTING_MUTATION_INTENT',{'setting':'kernel_timeout','original':'30','target':'60'})
   original_write=ns['write_all_bytes']
   def partial_write(fd,data):
    os.write(fd,b'{"event_type":');raise OSError(errno.ENOSPC,'mock failure after JSON prefix')
   ns['write_all_bytes']=partial_write
   try:j.append('WRITE_INTENT',{'offset_bytes':0,'attempt':1});raise AssertionError('append unexpectedly succeeded')
   except OSError:pass
   ns['write_all_bytes']=original_write
   if append_after_error:
    j.append('FATAL_ABORT',{'error':'mock append error'})
    j.append('RUN_CLOSURE',{'exit_code':1})
   try:j.reconcile_prior_records();raise AssertionError('torn journal accepted')
   except ns['ReallocatorError'] as exc:startup_error=str(exc)
   ns['assert_device_identity_and_capacity']=lambda *a:None
   ns['restore_recovery_settings']=lambda *a,**kw:(restores.append('called') or [])
   try:ns['reconcile_interrupted_journal_state'](d,0,j);raise AssertionError('torn journal recovered')
   except ns['ReallocatorError'] as exc:recovery_error=str(exc)
   assert 'Corrupted journal' in startup_error and 'Corrupted journal' in recovery_error and not restores
   add('partial journal append; later appends='+str(append_after_error),{'startup_error':startup_error,'recovery_error':recovery_error,'restoration_calls':restores,'journal':pathlib.Path(j.path).read_text()})
  finally:j.close();os.close(df)

print(json.dumps({'syntax':out['syntax'],'cases':len(out['cases']),'source_sha256':out['source_sha256']},indent=2))
