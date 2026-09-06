'use client';

import { ChangeEvent, useCallback, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, Download, RotateCcw, Save, UploadCloud } from 'lucide-react';

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button, buttonVariants } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Spinner } from '@/components/ui/spinner';
import { api, RestoreSystemBackupResult, SystemBackup } from '@/lib/api';


type RestoreTarget =
  | { kind: 'managed'; backup: SystemBackup }
  | { kind: 'upload'; file: File; backup: SystemBackup };


function dateLabel(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ko-KR');
}


function fileSizeLabel(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.ceil(bytes / 1024)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}


function reasonLabel(reason: SystemBackup['reason']) {
  if (reason === 'pre_restore') return '복구 직전 안전 백업';
  if (reason === 'manual') return '직접 만든 백업';
  return '검증되지 않은 백업';
}


export function BackupManager({
  mutationBlocked = false,
  blockedReason = '',
}: {
  mutationBlocked?: boolean;
  blockedReason?: string;
}) {
  const uploadInput = useRef<HTMLInputElement>(null);
  const loadSequence = useRef(0);
  const inspectSequence = useRef(0);
  const [open, setOpen] = useState(false);
  const [backups, setBackups] = useState<SystemBackup[]>([]);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  const [restoreTarget, setRestoreTarget] = useState<RestoreTarget | null>(null);
  const [restoreConfirmation, setRestoreConfirmation] = useState('');
  const [restoring, setRestoring] = useState(false);
  const [restoreResult, setRestoreResult] = useState<RestoreSystemBackupResult | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const loadBackups = useCallback(async () => {
    const sequence = ++loadSequence.current;
    setLoading(true);
    setError('');
    try {
      const result = await api.systemBackups();
      if (sequence === loadSequence.current) setBackups(result.backups);
    } catch (cause) {
      if (sequence === loadSequence.current) {
        setError(cause instanceof Error ? cause.message : '백업 목록을 불러오지 못했습니다.');
      }
    } finally {
      if (sequence === loadSequence.current) setLoading(false);
    }
  }, []);

  function mutationGuard() {
    if (!mutationBlocked) return true;
    setError(blockedReason || '저장하지 않은 내용이나 진행 중인 작업이 있습니다. 먼저 저장하거나 작업이 끝날 때까지 기다려 주세요.');
    return false;
  }

  async function createBackup() {
    if (creating || restoring || !mutationGuard()) return;
    setCreating(true);
    setError('');
    setNotice('');
    try {
      await api.createSystemBackup();
      setNotice('현재 검토 데이터와 원본 문서로 백업을 만들었습니다.');
      await loadBackups();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '백업을 만들지 못했습니다.');
    } finally {
      setCreating(false);
    }
  }

  async function chooseUploadedBackup(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file || !mutationGuard()) return;
    if (!/\.zip$/i.test(file.name)) {
      setError('ZIP 백업 파일만 사용할 수 있습니다.');
      return;
    }
    if (!file.size || file.size > 2 * 1024 * 1024 * 1024) {
      setError('백업 파일은 비어 있지 않은 2GB 이하 ZIP이어야 합니다.');
      return;
    }
    const sequence = ++inspectSequence.current;
    setError('');
    setNotice('');
    setInspecting(true);
    try {
      const result = await api.inspectUploadedSystemBackup(file);
      if (sequence !== inspectSequence.current) return;
      setRestoreConfirmation('');
      setRestoreTarget({ kind: 'upload', file, backup: result.backup });
    } catch (cause) {
      if (sequence === inspectSequence.current) {
        setError(cause instanceof Error ? cause.message : '외부 백업 파일을 검사하지 못했습니다.');
      }
    } finally {
      if (sequence === inspectSequence.current) setInspecting(false);
    }
  }

  function prepareManagedRestore(backup: SystemBackup) {
    if (!backup.restorable || !mutationGuard()) return;
    setError('');
    setNotice('');
    setRestoreConfirmation('');
    setRestoreTarget({ kind: 'managed', backup });
  }

  async function restoreBackup() {
    if (!restoreTarget || restoring || restoreConfirmation !== '복구' || !mutationGuard()) return;
    setRestoring(true);
    setError('');
    try {
      const result = restoreTarget.kind === 'managed'
        ? await api.restoreSystemBackup(restoreTarget.backup.id)
        : await api.restoreUploadedSystemBackup(restoreTarget.file);
      setRestoreResult(result);
      setRestoreTarget(null);
      setRestoreConfirmation('');
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : '백업을 복구하지 못했습니다.';
      setRestoreTarget(null);
      setRestoreConfirmation('');
      await loadBackups();
      setError(message);
    } finally {
      setRestoring(false);
    }
  }

  const targetLabel = restoreTarget?.kind === 'managed'
    ? dateLabel(restoreTarget.backup.created_at)
    : restoreTarget?.file.name || '';
  const targetBackup = restoreTarget?.backup;

  return (
    <>
      <Button
        variant="outline"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => {
          setOpen(true);
          setRestoreResult(null);
          void loadBackups();
        }}
      >
        <Save />데이터 백업
      </Button>

      <Dialog
        open={open}
        onOpenChange={(nextOpen) => {
          if (creating || restoring) return;
          if (!nextOpen && restoreResult) window.location.reload();
          setOpen(nextOpen);
          if (!nextOpen) {
            inspectSequence.current += 1;
            setInspecting(false);
            setError('');
            setNotice('');
            setRestoreTarget(null);
          }
        }}
      >
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>검토 데이터 백업</DialogTitle>
            <DialogDescription>
              프로젝트·판정·승인 기록과 참조 중인 원본 DOC/DOCX를 보관합니다. 외부 ZIP은 이 데이터 폴더의 서명 키와 일치할 때만 복구하며, API 키와 .env는 포함하지 않습니다.
            </DialogDescription>
          </DialogHeader>

          {restoreResult ? (
            <div className="space-y-4">
              <Alert>
                <CheckCircle2 />
                <AlertTitle>복구가 완료되었습니다</AlertTitle>
                <AlertDescription>
                  복구 직전 상태도 {dateLabel(restoreResult.safety_backup.created_at)} 안전 백업으로 보관했습니다.
                </AlertDescription>
              </Alert>
              <p className="text-sm text-muted-foreground">
                복구된 프로젝트와 판정 상태를 정확히 다시 불러오기 위해 화면을 새로 엽니다.
              </p>
            </div>
          ) : (
            <>
              {error && (
                <Alert variant="destructive">
                  <AlertTriangle /><AlertTitle>백업 작업을 진행하지 못했습니다</AlertTitle><AlertDescription>{error}</AlertDescription>
                </Alert>
              )}
              {notice && (
                <Alert>
                  <CheckCircle2 /><AlertTitle>백업 완료</AlertTitle><AlertDescription>{notice}</AlertDescription>
                </Alert>
              )}
              {mutationBlocked && (
                <p className="rounded-lg border border-border bg-muted/45 p-3 text-sm text-muted-foreground">
                  {blockedReason || '저장하지 않은 내용이나 진행 중인 작업이 있습니다. 먼저 저장하거나 작업이 끝날 때까지 기다려 주세요.'}
                </p>
              )}

              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-sm text-muted-foreground">최근 백업 20개를 이 PC에 보관합니다.</p>
                <div className="flex flex-wrap gap-2">
                  <input
                    ref={uploadInput}
                    type="file"
                    accept=".zip,application/zip"
                    className="sr-only"
                    onChange={(event) => void chooseUploadedBackup(event)}
                    disabled={mutationBlocked || loading || creating || inspecting || restoring}
                  />
                  <Button
                    variant="outline"
                    onClick={() => uploadInput.current?.click()}
                    disabled={mutationBlocked || loading || creating || inspecting || restoring}
                  >
                    {inspecting ? <Spinner /> : <UploadCloud />}{inspecting ? '백업 검사 중' : '외부 백업 복구'}
                  </Button>
                  <Button onClick={() => void createBackup()} disabled={mutationBlocked || loading || creating || inspecting || restoring}>
                    {creating ? <Spinner /> : <Save />}{creating ? '백업 중' : '지금 백업 만들기'}
                  </Button>
                </div>
              </div>

              <div className="max-h-[52vh] space-y-2 overflow-y-auto pr-1">
                {loading ? (
                  <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed p-8 text-sm text-muted-foreground">
                    <Spinner />백업 목록을 확인하고 있습니다.
                  </div>
                ) : backups.length === 0 ? (
                  <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
                    아직 만든 백업이 없습니다. ‘지금 백업 만들기’를 눌러 첫 백업을 만드세요.
                  </div>
                ) : backups.map((backup) => (
                  <div key={`${backup.id}-${backup.sha256}`} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="font-medium">{dateLabel(backup.created_at)}</p>
                        <Badge variant={backup.restorable ? 'outline' : 'destructive'}>{reasonLabel(backup.reason)}</Badge>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground" title={`SHA-256 ${backup.sha256}`}>
                        프로젝트 {backup.project_count}개 · 원본 {backup.upload_count}개 · {fileSizeLabel(backup.size_bytes)}
                        {!backup.restorable ? ` · ${backup.error || '검증 실패'}` : ''}
                      </p>
                    </div>
                    <div className="flex gap-2">
                      {backup.restorable && (
                        <a className={buttonVariants({ variant: 'outline', size: 'sm' })} href={api.systemBackupUrl(backup.id)}>
                          <Download />내려받기
                        </a>
                      )}
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={!backup.restorable || mutationBlocked || creating || restoring}
                        onClick={() => prepareManagedRestore(backup)}
                      >
                        <RotateCcw />이 시점으로 복구
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          <DialogFooter>
            {restoreResult ? (
              <Button onClick={() => window.location.reload()}>복구된 화면 다시 열기</Button>
            ) : (
              <Button variant="outline" onClick={() => setOpen(false)} disabled={creating || restoring}>닫기</Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={restoreTarget !== null}
        onOpenChange={(nextOpen) => {
          if (!nextOpen && !restoring) {
            setRestoreTarget(null);
            setRestoreConfirmation('');
          }
        }}
      >
        <AlertDialogContent className="max-w-lg">
          <AlertDialogHeader>
            <AlertDialogTitle>{targetLabel} 상태로 되돌릴까요?</AlertDialogTitle>
            <AlertDialogDescription>
              현재 검토 데이터는 이 백업을 만든 시점의 상태로 바뀝니다. 그 이후 추가하거나 수정한 프로젝트·판정·승인 기록은 화면에서 사라집니다. 복구 직전 현재 상태는 자동으로 안전 백업합니다.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {targetBackup && (
            <div className="rounded-lg border border-border bg-muted/45 p-3 text-sm">
              <p className="font-medium">검증된 백업 정보</p>
              <p className="mt-1 text-muted-foreground">
                생성 {dateLabel(targetBackup.created_at)} · 프로젝트 {targetBackup.project_count}개 · 원본 {targetBackup.upload_count}개 · {fileSizeLabel(targetBackup.size_bytes)}
              </p>
            </div>
          )}
          {mutationBlocked && (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>지금은 복구할 수 없습니다</AlertTitle>
              <AlertDescription>
                {blockedReason || '저장하지 않은 내용이나 진행 중인 작업이 있습니다. 먼저 저장하거나 작업이 끝날 때까지 기다려 주세요.'}
              </AlertDescription>
            </Alert>
          )}
          <label className="block text-sm font-medium" htmlFor="backup-restore-confirmation">
            계속하려면 ‘복구’를 입력하세요.
            <input
              id="backup-restore-confirmation"
              className="mt-2 h-10 w-full rounded-lg border border-input bg-background px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:border-ring focus:ring-3 focus:ring-ring/50"
              value={restoreConfirmation}
              onChange={(event) => setRestoreConfirmation(event.target.value)}
              placeholder="복구"
              autoComplete="off"
              disabled={restoring}
            />
          </label>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={restoring}>취소</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={mutationBlocked || restoring || restoreConfirmation !== '복구'}
              onClick={() => void restoreBackup()}
            >
              {restoring ? <Spinner /> : <RotateCcw />}{restoring ? '복구 중' : '이 백업으로 복구'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
