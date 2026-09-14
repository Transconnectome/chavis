# 차지욱 철학 에이전트: 원천 수집과 지속 갱신 API 조사

조사일: 2026-09-12. 장르: 구현 의사결정을 위한 기술 조사 보고서. 범위: Google Drive·Docs, Gmail, Microsoft Teams의 공식 API 계약. 비공개 원문·인증정보를 조회하지 않았으며, 여기의 API 지원 여부는 실제 계정 권한이나 수집 완료를 뜻하지 않는다. 공식 개발자 문서 본문을 확인했고, 검색 결과 요약만으로 내린 결론은 포함하지 않았다.

## 1. 권고 아키텍처와 완료 기준

**권고: 읽기 전용 수집기 → 출처·작성자·맥락 원장 → 근거가 연결된 철학 후보 → 검증된 적용 규칙**으로 분리한다. 글을 모두 한 벡터 저장소에 넣은 뒤 차교수의 철학으로 부르는 구조는 작성자 오인, 반복 인용의 과대 계산, 삭제 자료의 잔존을 통제하기 어렵다. 다음 설계는 공식 API의 보장과 한계를 바탕으로 한 이 프로젝트의 구현 판단이다.

1. 수집기는 승인된 계정과 자료 범위를 버전 관리한다. Google 계정, Drive 폴더·파일·공유 드라이브, 연구실 멤버 이메일 및 재적 기간, Teams tenant·team·channel·chat 식별자를 서로 다른 필드로 둔다.
2. 원문의 출처 시각, 수집 시각, 내용 버전, 작성자 식별 근거를 분리한다. `direct_professor`, `context_only`, `joint_or_unknown`, `quoted_or_forwarded`, `system_event`가 최소 구분이다.
3. 알림은 다시 읽을 대상을 알려주는 신호로 취급한다. 페이지 전체를 저장한 뒤에만 다음 커서를 확정한다. 실패하면 재처리해도 동일한 결과를 얻어야 한다.
4. 원천 삭제·접근권 상실·범위 제외는 검색 결과와 파생 철학의 근거 연결에도 전파한다. 삭제를 교수의 견해 철회로 해석하지 않는다.
5. 실행 상태를 `bootstrap_partial`, `caught_up`, `degraded`, `reauth_required`, `scope_reconciliation_required`로 표시한다. 성공한 API 호출 하나를 ‘전체 파악’으로 보고하지 않는다.

‘전체 파악’의 검증 가능한 정의는 **선언된 계정·기간·자료 범위에서 현재 API가 열거하는 객체와 모든 페이지·답글을 처리했고, 미지원 형식·작성자 불명·삭제·권한 제한·과거 이력 누락을 계수했다**는 것이다. 이미 삭제된 원문이나 API에 노출되지 않는 과거 수정 이력까지 복원했다는 뜻은 아니다.

개인용 1차 배포는 로컬 주기 실행과 SQLite 트랜잭션으로 충분하다. 웹훅은 지연을 줄일 필요가 실제로 확인될 때 추가한다. Gmail은 Pub/Sub pull도 지원하므로 수집을 위해 이 머신의 웹 서비스를 공개할 이유가 없다. [Gmail push](https://developers.google.com/workspace/gmail/api/guides/push)

## 2. 공통 데이터 계약

다음은 API가 제공하는 단일 공통 스키마가 아니라 구현 권고이다. 본문을 보존할 수 있는 근거와 기간은 자료별로 기록한다.

| 필드 | 목적 |
|---|---|
| `source_key` | provider + account/tenant + container + parent + native ID의 충돌 없는 키 |
| `provider`, `account_id`, `tenant_id` | 동일 제목·동일 표시명·다중 계정 혼동 방지 |
| `container_id`, `thread_id`, `parent_id`, `native_id` | 문서·메일 thread·Teams root/reply 관계 복원 |
| `author_id`, `author_display`, `authorship_basis` | 실제 계정 ID와 표시명을 분리하고 판정 근거 보존 |
| `authorship_status` | 교수 직접 발언 / 맥락 / 공동·불명 / 인용 / 시스템 |
| `body`, `authored_spans`, `quoted_spans` | 원문과 화자별 추출 구간 분리; 구간은 원문 offset에 연결 |
| `source_created_at`, `source_modified_at`, `observed_at` | 발언 시점과 수집 시점 구별 |
| `native_version`, `body_sha256`, `parser_version` | 메타데이터 변경과 내용 변경 구별; 재현 가능한 파싱 |
| `source_url`, `locator` | 원문 열기와 tab·문단·comment·message 위치 |
| `scope_version`, `access_checked_at`, `availability` | 수집 범위·현재 접근 상태와 근거 재사용 조건 |
| `retention_basis`, `expires_at`, `reuse_scope` | 사용자 소유 자료와 타인 대화 맥락의 보존·사용 조건 |
| `evidence_edges` | 후보 원칙에서 정확한 원천 버전·구간으로 역추적 |

체크포인트는 `provider/account/container/scope_version`별로 관리한다. `committed_cursor`, `pending_page_cursor`, `generation`, `last_success_at`, `last_full_reconciliation_at`, `last_error_class`를 분리하면 실패한 페이지를 건너뛰거나 다른 범위의 커서를 재사용하는 문제를 방지할 수 있다. SQL·로그에는 토큰 원문을 출력하지 않는다.

## 3. Google Drive와 Docs

### 3.1 권한과 범위

전체 접근 가능 Drive 내용의 읽기에는 `drive.readonly`가 맞는다. `drive.metadata.readonly`만으로 본문을 읽을 수는 없다. `drive.file`은 사용자가 앱에 개별 공유·선택한 파일에 한정되며 수정 가능성도 포함하므로, ‘기존 Drive 전체를 읽기 전용으로 탐색’하는 권한과 같지 않다. 두 범위의 차이를 숨기지 않는다. [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)

목록 수집은 명시된 corpus별로 실행한다. 공유 드라이브는 해당 `driveId`와 관련 지원 옵션을 사용하고, 목록 응답의 `incompleteSearch`가 참이면 전체 수집 완료를 선언하지 않는다. `files.list`는 휴지통 파일도 기본으로 포함하므로 목적에 맞는 `trashed` 정책을 명시한다. 이름이나 키워드 검색만으로 전체 범위를 정의하지 않는다. [files.list](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list)

권고: 폴더 하위 재귀 탐색은 부모 ID 그래프를 사용한다. 바로가기 대상이 범위 밖이면 자동 확장하지 않고 발견 목록에 남긴다. 공유 문서의 접근 가능성과 교수의 직접 저술 여부를 분리한다.

### 3.2 작성자 귀속의 한계

파일의 `owners`는 소유자이며 `lastModifyingUser`는 마지막 수정자다. 공유 드라이브 파일은 `owners`가 채워지지 않고, `headRevisionId`는 현재 바이너리 파일에만 제공된다. 이 필드들은 문장별 저자 인증이 아니다. **차교수가 소유하거나 마지막으로 수정한 공동 문서 전체를 교수의 발언으로 승격하지 않는다.** [File resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)

Drive 댓글은 `author`, `content`, `createdTime`, `modifiedTime`, `resolved`, `deleted`, `quotedFileContent`, `replies`를 제공한다. `modifiedTime`은 댓글 또는 답글의 마지막 변경을 반영한다. 특히 `author.emailAddress`와 `author.permissionId`는 채워지지 않는다고 명시돼 있다. `quotedFileContent`는 댓글 대상 문맥이며 댓글 작성자의 말로 보지 않는다. [Comment resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/comments)

`User.me`는 요청 사용자인지 표시한다. 따라서 인증 계정이 교수 본인의 검증된 계정일 때 `author.me=true`를 강한 귀속 근거로 사용할 수 있다. 응답에 그 값이 없거나 다른 계정으로 수집했다면 표시명만으로 확정하지 않는다. 여러 교수 계정은 각각 검증한다. [Drive User](https://developers.google.com/workspace/drive/api/reference/rest/v3/User)

답글도 작성자 이메일·permission ID가 생략된다. `action=resolve/reopen`은 토론 상태 변화이고 찬성·철학 확정의 표시가 아니다. 삭제된 답글에는 내용이 없다. [Reply resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/replies)

### 3.3 본문·댓글·이력 수집

Docs는 `documents.get(includeTabsContent=true)`를 사용하고 `tabs[].childTabs`를 재귀 순회해야 한다. 기본 요청은 첫 번째 tab만 반환한다. `tab_id`와 문단·표·각주 위치를 원문 locator에 남긴다. ‘본문을 받았다’는 성공 판정으로 다른 tab 누락을 감추지 않는다. [Docs tabs](https://developers.google.com/workspace/docs/api/how-tos/tabs)

댓글 API의 `fields`는 필수다. Workspace 편집기의 내부 anchor는 불투명 데이터이므로 Drive API만으로 문서 위치를 정확히 해석한다고 가정할 수 없다. `quotedFileContent`와 실제 문서 구간의 일치를 확인하고, 중복 문구·사라진 문구·빈 anchor는 `locator_unresolved`로 표시한다. [Comments guide](https://developers.google.com/workspace/drive/api/guides/manage-comments)

`comments.list`에는 `includeDeleted=true`, `startModifiedTime`, `pageToken`이 있다. 최대 페이지 크기는 100이다. 삭제 댓글은 과거 내용을 반환하지 않는다. 다음 page token이 거절되면 처음부터 다시 열거한다. 댓글별 수정 시각에 중첩 구간을 두고 재수집하면 경계 시각 누락을 줄일 수 있다. 별도 댓글 주기 수집으로 본문 변경 감지에만 의존하지 않는다. [comments.list](https://developers.google.com/workspace/drive/api/reference/rest/v3/comments/list)

`revisions.list`는 수정 이력이 많은 Docs·Sheets·Slides의 오래된 revision을 생략할 수 있고, UI의 이력이 API보다 완전할 수 있다. 따라서 과거 전 기간의 문장 저자 복원은 보장하지 않는다. 읽기 수집기가 revision 보존을 위해 `keepForever`를 쓰는 것은 원천 변경이므로 이 구현에 포함하지 않는다. [Revisions guide](https://developers.google.com/workspace/drive/api/guides/manage-revisions)

### 3.4 초기 수집과 증분 동기화

Drive `changes`는 변경된 파일의 현재 상태를 제공한다. `changes.getStartPageToken`으로 경계를 얻고 `changes.list`의 모든 페이지를 처리한다. `changes.watch` 알림에는 변경 본문이 없으므로 실제 변경은 feed에서 가져와야 한다. [Retrieve changes](https://developers.google.com/workspace/drive/api/guides/manage-changes)

권고 bootstrap 순서:

1. 범위와 인증 계정을 고정한 다음 시작 토큰 `T0`를 얻는다.
2. 해당 범위의 파일 목록·본문·댓글을 별도 bootstrap generation에 수집한다.
3. **수집 전에 얻은 `T0`부터** 변경 feed를 끝까지 재생해 수집 도중 변경을 반영한다.
4. 모든 저장·제외·실패 상태가 반영됐을 때 generation을 활성화한다. 실패 객체를 성공 객체처럼 숨기지 않는다.

`nextPageToken`이 있으면 계속 진행하고, 마지막 페이지의 `newStartPageToken`을 다음 주기의 경계로 확정한다. Drive 변경 토큰은 만료되지 않는다고 명시돼 있다. 이를 Gmail 이력 만료 규칙과 혼동하지 않는다. `includeRemoved=true` 및 공유 드라이브 범위 옵션을 명시한다. `restrictToMyDrive=true`는 My Drive에 추가되지 않은 공유 파일 등을 제외할 수 있다. [changes.list](https://developers.google.com/workspace/drive/api/reference/rest/v3/changes/list)

`removed=true`는 파일 삭제와 접근권 상실을 모두 포함한다. 삭제라고 단정하지 않고 `source_unavailable`을 기록해 검색·철학 근거 사용을 중지한다. `file`이 없는 removal도 정상 경로다. [Change resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/changes)

권고: 폴더 이동과 앱의 범위 변경도 주기적으로 재대조한다. feed가 폴더 allowlist 자체를 대신하지 않는다. 권한 변경으로 접근이 사라지면 파생 규칙의 근거 수를 재계산하고, 유일 근거가 사라진 규칙은 다시 검토 대상으로 전환한다.

## 4. Gmail: 교수 발언과 연구실 대화 맥락

### 4.1 최소 권한과 후보 발견

본문 읽기는 `gmail.readonly`를 사용한다. `gmail.metadata`는 본문 접근이 없고 `gmail.modify`, `gmail.compose`, `mail.google.com`은 필요 이상의 변경·발송 기능을 포함한다. 연구실 메일 분석 수집기에 발송 권한은 필요하지 않다. [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)

API 검색은 Gmail UI처럼 이메일 alias를 자동 확장하지 않고 thread 전체를 대상으로 한 검색도 같지 않다. 교수의 모든 검증된 발신 주소와 연구실 멤버 주소·그룹 주소를 명시해야 한다. API 검색의 날짜 문자열 시간대 차이를 피하려면 UTC epoch 기반 경계를 쓰고 원본 시간도 보존한다. [Search and filter](https://developers.google.com/workspace/gmail/api/guides/filtering)

권고 발견 조건은 `SENT + verified From + lab recipient/time membership`이며, `from:` 단독은 충분하지 않다. 수신함의 위조·전달 발신자나 다른 계정의 가져온 메일과 구별한다. 당시 연구실 멤버였는지 여부를 시간 구간으로 관리해야 졸업생·합류 전 연락을 잘못 분류하지 않는다. 그룹 주소는 대상 집단 근거를 별도로 둔다.

후보 메시지의 `threadId`를 이용해 `threads.get`으로 현재 접근 가능한 대화 전체를 가져온다. API는 thread 내 메시지를 순서대로 제공한다. 교수 메시지 하나만 읽으면 ‘이 방식으로 하세요’가 무엇을 가리켰는지 놓치고, 다른 사람의 의견을 교수 철학으로 섞을 수 있다. [Threads guide](https://developers.google.com/workspace/gmail/api/guides/threads)

### 4.2 MIME과 인용 구간

`Message.id`는 불변 식별자다. `snippet`은 짧은 일부 텍스트라 근거로 사용하지 않는다. `format=RAW`는 전체 RFC 2822 메일을 base64url로 제공하고, `FULL`은 MIME 구조와 헤더를 제공한다. `internalDate`는 일반 SMTP 수신의 Google 접수 시각이지만 이관 메일에서는 Date 기반으로 설정될 수 있다. 따라서 Date 헤더도 별도로 남긴다. [Message resource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages)

권고 파서:

- RFC 헤더 디코딩, charset 처리, multipart 재귀, HTML/plain 대안 중복 제거를 수행한다.
- `From`, `Sender`, `To`, `Cc`, `Bcc`가 제공되는 범위, `Message-ID`, `In-Reply-To`, `References`, `Date`를 구조화한다.
- 본문에 포함된 `On ... wrote:`, `-----Original Message-----`, 한국어 회신 구분, HTML `blockquote`, 서명을 후보 경계로 분류하되 원문을 삭제하지 않는다.
- 문단 사이 답변은 모든 첫 인용 구간 뒤 텍스트를 버리는 방식으로 처리하지 않는다. `authored_spans`와 `quoted_spans`를 원문 offset·근거와 함께 둔다.
- 반복 인용된 교수 문장은 원출처 message ID·내용 hash에 연결한다. 같은 말을 다른 사람이 여러 번 인용해도 독립 발언 수는 늘리지 않는다.
- 교수 발송 메일에 첨부된 공동 원고는 메일 본문과 다른 출처다. 보낸 사람이 첨부 문장 전부를 작성했다고 추정하지 않는다.

이 파서는 Gmail이 제공하는 ‘원저자 자동 추출’ 기능이 아니다. 실제 한국어·영어·모바일·인라인 회신을 포함한 고정 사례로 검증해야 하는 자체 구성요소다.

### 4.3 이력 기반 지속 갱신

공식 full sync는 목록에서 메시지를 열거하고 FULL/RAW를 저장한 뒤 이력을 이용한 partial sync로 이어진다. 이력은 보통 일주일 이상 유지되지만 더 짧거나 일시적으로 사용 불가능할 수 있다. 범위를 벗어난 `startHistoryId`는 404를 반환하며 full sync가 필요하다. [Gmail sync](https://developers.google.com/workspace/gmail/api/guides/sync)

bootstrap race를 막기 위한 구현 권고:

1. `users.getProfile`에서 계정과 현재 mailbox `historyId=H0`를 저장한다. [getProfile](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile)
2. 후보 목록의 모든 페이지와 관련 thread를 수집한다.
3. `history.list(startHistoryId=H0)`를 끝까지 재생한다. bootstrap 후의 최신 프로필 ID로 시작하면 bootstrap 도중 사건을 건너뛸 수 있다.
4. `H0`가 bootstrap 중 만료됐다면 `bootstrap_stale`로 표시하고 새 generation을 만든다. 긴 초기 수집에서는 history를 병행 배출해 변경 ID를 임시 원장에 보존한다.

History ID는 증가하지만 연속적이지 않으므로 직접 1을 더하거나 숫자 범위를 생성하지 않는다. `messagesAdded`, `messagesDeleted`, `labelsAdded`, `labelsRemoved`를 사용한다. 일반 `messages`와 중복이 있을 수 있다. `messagesDeleted`는 휴지통 이동이 아닌 실제 삭제다. 마지막 페이지에서 반환된 이력을 확정한다. [history.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list)

권고: mailbox 이력은 읽되 본문 fetch와 영구 저장은 교수·연구실 범위로 제한한다. `SENT`만 알림 필터로 사용하면 관련 thread에 도착한 학생 답변을 놓칠 수 있으므로, 승인된 thread의 새 메시지 여부도 평가한다. 삭제 이벤트는 알려진 source ID를 우선 대조한다. 휴지통·스팸 제외 정책은 라벨 변경에서 재평가한다.

Push를 쓴다면 `watch`의 응답 경계와 만료 시각을 저장하고, 공식 권고대로 매일 갱신한다. 최소 7일마다 갱신하지 않으면 알림이 중단된다. 알림 누락 가능성이 있어 무알림 상태에서도 `history.list` polling을 계속한다. 새 알림 ID를 곧바로 완료 커서로 덮어쓰지 않는다. [Gmail push](https://developers.google.com/workspace/gmail/api/guides/push)

## 5. Microsoft Teams

### 5.1 채널 thread와 chat은 다른 API 범위

채널은 `GET /teams/{team-id}/channels/{channel-id}/messages`로 root를 열거한다. 기본 결과에는 답글이 없다. `$expand=replies` 응답에는 최대 200개의 답글이 들어가며 초과분은 `replies@odata.nextLink`로 따라간다. root 페이지의 `@odata.nextLink`와 답글의 nextLink를 별도로 모두 처리한다. root 정렬은 root·답글 전체 chain의 최종 수정 시각 기준이다. 지원 query는 `$top`, `$expand`이고 임의 author/date 필터를 붙이지 않는다. [List channel messages](https://learn.microsoft.com/en-us/graph/api/channel-list-messages?view=graph-rest-1.0)

별도 답글 API는 `GET /teams/{team-id}/channels/{channel-id}/messages/{message-id}/replies`이며 `$top` 최대값은 50이다. `ChannelMessage.Read.Group` application RSC 또는 delegated `ChannelMessage.Read.All` 등 endpoint가 지원하는 권한을 사용한다. [List replies](https://learn.microsoft.com/en-us/graph/api/chatmessage-list-replies?view=graph-rest-1.0)

chat은 `GET /chats/{chat-id}/messages`로 별도 처리한다. delegated 최소 권한은 `Chat.Read`, application의 제한된 권한은 `ChatMessage.Read.Chat`이다. 날짜 필터는 같은 속성의 `$orderby`와 함께 써야 하며 그렇지 않으면 필터가 무시된다. `$top` 최대 50, 내림차순만 지원한다. [List chat messages](https://learn.microsoft.com/en-us/graph/api/chat-list-messages?view=graph-rest-1.0)

권고: scope는 교수 참여 chat와 연구실 channel allowlist로 선언한다. 교수 이름 검색만으로 발견하면 교수 답글만 있는 타인 시작 thread와 이름이 없는 메시지를 놓친다. channel은 전체 root/replies를 확인한 후 직접 교수 발언을 선택하고, 타인 메시지는 관련 맥락으로만 연결한다.

### 5.2 ID와 저자·편집·삭제

Teams message ID는 chat/channel/reply-parent 안에서만 고유하다. `tenant/container/parent/message` 복합키를 쓴다. `from`의 user ID로 교수를 식별하며 displayName은 표시용이다. `lastModifiedDateTime`은 reaction 변경도 포함하고 `lastEditedDateTime`은 본문 편집을 구별한다. `deletedDateTime`은 삭제 시각이다. `replyToId`는 channel에 적용하며 chat에서 thread 구조를 제공한다고 가정하지 않는다. [chatMessage resource](https://learn.microsoft.com/en-us/graph/api/resources/chatmessage?view=graph-rest-1.0)

권고: Teams 보관 DB에 이름만 있으면 `authorship=unverified_display_name`으로 가져온다. 원천 ID로 live refetch한 후 작성자 ID를 보완한다. 원천 ID까지 없으면 정확한 내용·시간·container 매칭을 검토하고 자동 확정하지 않는다. `from.application`, system event, 대리 게시·migration 자료는 별도 상태로 둔다. reaction은 철학의 반복 증거나 동의로 계산하지 않는다. HTML body의 mention과 첨부 링크를 보존하되 외부 링크가 포함됐다는 이유로 자동 크롤링하지 않는다.

### 5.3 delta의 정확한 범위

`GET /users/{id}/chats/getAllMessages/delta`는 사용자가 참여하는 chat의 신규·변경 메시지를 추적한다. **delegated는 지원하지 않고 application `Chat.Read.All`이 필요하며 최근 8개월까지만 반환한다.** URL의 opaque `@odata.nextLink`를 그대로 따라가고 terminal `@odata.deltaLink`를 다음 주기에 사용한다. 이 API를 channel용 delta로 재사용할 수 없다. 문서가 삭제 사건의 완전성을 명시하지 않으므로 delta만으로 삭제 동기화가 완전하다고 주장하지 않는다. [Chat delta](https://learn.microsoft.com/en-us/graph/api/chatmessage-delta?view=graph-rest-1.0)

더 오래된 사용자 chat는 `GET /users/{id}/chats/getAllMessages` 또는 허용된 개별 chat 목록으로 backfill한다. getAllMessages도 application `Chat.Read.All`이 필요하다. 사용자 ID 발신 필터가 지원되지만 직접 발언만 받으면 맥락 수집을 추가해야 한다. [getAllMessages](https://learn.microsoft.com/en-us/graph/api/chats-getallmessages?view=graph-rest-1.0)

Graph delta 일반 계약은 replay와 전파 지연 가능성을 명시한다. `410 Gone` 또는 `syncStateNotFound` 등 reset은 해당 자원 full sync로 처리한다. 문서에 없는 Teams delta 만료 일수를 다른 resource의 7일 규칙에서 가져오지 않는다. [Delta overview](https://learn.microsoft.com/en-us/graph/delta-query-overview)

권고 기본 경로는 제한된 chat/channel 주기 수집이다. 필요한 권한이 실제로 부여된 경우에만 user chat delta를 추가한다. channel 목록에는 date filter가 없으므로 오래된 thread의 최신 답글·수정까지 확인하는 재대조가 필요하다. 정렬이 같아 보이는 일부 페이지를 보고 임의로 조기 종료하면 그 범위의 누락 위험을 계수해야 한다.

### 5.4 알림·권한 범위·운영

channel별 `/teams/{team-id}/channels/{channel-id}/messages`, chat별 `/chats/{chat-id}/messages` 구독을 우선한다. 1시간 넘는 만료 시각을 요청할 때 `lifecycleNotificationUrl`이 필수다. 알림 뒤 GET은 알림 당시 snapshot이 아니라 현재 상태를 반환한다. 짧은 시간의 여러 편집을 모두 원문 이력으로 복원할 수 있다고 주장하지 않는다. [Teams message notifications](https://learn.microsoft.com/en-us/graph/teams-changenotifications-chatmessage)

Teams `chatMessage` 구독의 최대 수명은 3일이며, resource data를 포함하는 rich notification은 하루 미만이다. 반환된 실제 만료 시각 기준으로 선제 갱신한다. [Subscription resource](https://learn.microsoft.com/en-us/graph/api/resources/subscription?view=graph-rest-1.0)

`reauthorizationRequired`는 모든 resource에서, `subscriptionRemoved`는 Teams chatMessage에서 지원한다. **`missed` lifecycle 알림의 지원 목록은 Outlook이며 Teams가 아니다.** 그러므로 Teams가 누락 통지를 보내 줄 것이라는 전제로 polling을 생략하지 않는다. 제거 후 재구독 사이의 공백은 앱이 스스로 다시 동기화해야 한다. [Lifecycle events](https://learn.microsoft.com/en-us/graph/change-notifications-lifecycle-events)

RSC는 설치된 특정 team/chat의 자원으로 application 권한을 좁힐 수 있다. `ChannelMessage.Read.Group`은 해당 team 채널 메시지 접근이다. 조직 전체 `ChannelMessage.Read.All`/`Chat.Read.All`을 편의상 먼저 요청하지 않는다. 실제 앱 설치·resource 동의·tenant 정책을 확인하며 RSC 지원이 모든 endpoint에 동일하다고 가정하지 않는다. [RSC](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/resource-specific-consent)

과거 Teams export API의 metered model A/B 정보를 현재 비용으로 재사용하면 안 된다. 공식 공지는 2025-08-25부터 해당 API의 metering과 billing configuration이 제거됐고 `model`이 무시된다고 명시한다. 개별 API의 현재 예외는 그 endpoint 문서에서 확인한다. 무료 호출량이나 예산은 여기서 추정하지 않는다. [Teams API billing notice](https://learn.microsoft.com/en-us/graph/teams-licenses)

## 6. 인증·원문 보존·파생 철학의 사용 범위

지속 Google 수집에는 OAuth offline access와 refresh token 갱신이 필요하다. refresh token은 비밀 저장소에서만 읽고 만료·회수 실패를 새 동의가 필요한 상태로 보고한다. 같은 서비스의 기존 인증을 재사용할 수 있으나 granted scopes가 읽기만인지 별도로 확인한다. [Google OAuth offline access](https://developers.google.com/identity/protocols/oauth2/web-server)

본 과업은 사용자가 요청한 교수 개인화 기능이다. 현재 Google Workspace 정책은 해당 사용자의 개인화 모델을 넘는 AI 개발·훈련 목적 사용을 제한하며 파생 데이터에도 Limited Use를 적용한다. 원문과 토큰의 저장·전송 보호, 사용 목적 고지, 관리·삭제 수단을 제품 계약에 포함한다. 이 조건을 모든 개인화가 금지된다는 뜻으로 확대하지 않는다. [Workspace user data policy](https://developers.google.com/workspace/workspace-api-user-data-developer-policy)

원문을 무조건 영구 보존하는 구조도 기본값으로 삼지 않는다. Google API 약관 §5(e)는 콘텐츠 소유자의 명시 허용 또는 법률상 허용을 전제로 예외를 둔다. 교수 본인의 글·발언과 타인 소유 대화 맥락·공동 문서는 다르므로 보존 근거를 구분한다. 사용자 자신의 글에 대한 현재 요청을 무시해 매번 동의를 다시 묻는 절차는 필요하지 않다. [Google API terms §5](https://developers.google.com/terms#section_5_content)

구현 권고는 `retention_basis`를 가진 원문 증거와 만료 가능한 맥락 cache를 분리하고, 철학·리뷰 규칙의 근거 연결을 유지하는 것이다. 특정 연구실 구성원의 평가나 사적인 상황은 교수의 일반 원칙과 다르다. 일반 글쓰기·평가·리뷰 규칙으로 전사할 때 인명·사건 세부사항을 포함할 이유가 있는지 별도 판정한다. 공유 범위가 넓은 결과에는 기존 비공개 대화를 재노출하지 않는다.

### 6.1 Teams delegated 인증 실행 계약 — 2026-09-13 추가 조사

**권고는 전용 public-client 앱의 최초 사용자 로그인과 이후 무인 갱신을 분리하는 것이다.** 조사 시 `TeamsGraphClient.from_env()`는 `CHA_TEAMS_ACCESS_TOKEN` 한 값을 보관하고 `get_json()`에서 재사용한다. `sync_teams()`는 이 생성자를 직접 호출하고, 설치기는 해당 환경변수를 현재 사용자 systemd manager로 가져올 수 있을 뿐 refresh token을 관리하지 않는다. 기존 설정·합성 설정 예시의 `teams_tenant_id`, `teams_professor_ids`, `teams_team_ids`, `teams_include_chats`는 자료 범위이며 앱 등록 정보가 아니다. 아래는 구현 계약이며, 앱 등록·동의·로그인·실제 캐시 접근 또는 무인 동작을 완료했다는 기록이 아니다. 후속 구현에서 `from_config()`의 계정에 묶인 token provider, `teams-auth status/login`, 정기 실행 연결과 실제 MSAL 캐시 코드에 대한 회귀 검사를 추가했다. 외부 인증 미수행 상태는 그대로다.

**계정과 권한.** 이 agent를 위해 승인된 앱 ID와 tenant GUID를 사용해 `PublicClientApplication`을 만든다. authority는 해당 tenant로 고정하고, 앱 등록의 public client flow 허용이 필요하다. device-code 방식에 client secret이나 기존 다른 앱의 ID·캐시가 필요하지 않다. 최초 로그인은 사용자가 준비되었을 때만 `initiate_device_flow` → `acquire_token_by_device_flow`로 실행한다. 로그인 후 예상 tenant와 단일 교수 object ID를 대조하고, `/me?$select=id` 결과가 다르면 수집을 시작하지 않는다. 이름·메일 표시값이나 cache의 첫 계정을 자동 선택하지 않는다. [Public client 등록](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration), [MSAL token acquisition](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens), [Get user](https://learn.microsoft.com/en-us/graph/api/user-get?view=graph-rest-1.0)

| 수행할 Graph 읽기 | 필요한 delegated scope | 공식 권한표의 관리자 동의 | 현재 구현에서의 필요성 |
|---|---|---|---|
| `/me?$select=id`로 로그인 계정 확인 | `User.Read` | No | 새 인증 경로의 계정 검증 |
| `/users/{교수 ID}/chats` 및 `/chats/{chat ID}/messages` | `Chat.Read` | No | chat 목록과 본문을 함께 충족. 목록만 읽는 `Chat.ReadBasic` 추가 불필요 |
| 설정된 team의 `/teams/{team ID}/channels` | `Channel.ReadBasic.All` | No | 현재 채널 목록 경로에 필요 |
| channel root 및 replies | `ChannelMessage.Read.All` | **Yes** | 현재 채널 본문 경로에 필요 |
| `/me/joinedTeams` | `Team.ReadBasic.All` | No | 향후 team 목록 발견을 구현할 때만 추가. 현재 team allowlist에는 불필요 |

위 scope 선택은 endpoint별 최소 권한과 공식 permission reference를 대조했다. 현재 경로의 요청 집합은 `User.Read`, `Chat.Read`, `Channel.ReadBasic.All`, `ChannelMessage.Read.All`이다. `Chat.Read.All` application 권한, `Group.Read.All`, 쓰기·발송 권한은 이 계약에 포함하지 않는다. 표의 No는 tenant가 사용자 동의를 허용한다는 보장이 아니다. 조직의 consent policy·앱 등록 제한에 따라 관리자 절차가 추가될 수 있다. 로그인 성공도 사라진 과거 멤버십이나 접근 불가능한 private/shared channel의 전체 목록을 보장하지 않는다. [List chats](https://learn.microsoft.com/en-us/graph/api/chat-list?view=graph-rest-1.0), [Chat messages](https://learn.microsoft.com/en-us/graph/api/chat-list-messages?view=graph-rest-1.0), [List channels](https://learn.microsoft.com/en-us/graph/api/channel-list?view=graph-rest-1.0), [Channel messages](https://learn.microsoft.com/en-us/graph/api/channel-list-messages?view=graph-rest-1.0), [Joined teams](https://learn.microsoft.com/en-us/graph/api/user-list-joinedteams?view=graph-rest-1.0), [Permission reference](https://learn.microsoft.com/en-us/graph/permissions-reference), [Tenant user consent policy](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-user-consent)

OAuth 프로토콜에서 refresh token에는 `offline_access`가 필요하다. MSAL Python은 이를 기본 추가하므로 Graph 권한 목록에 reserved OIDC scope를 수동으로 섞지 않고, `exclude_scopes`로 `offline_access`를 제거하지 않는다. 실제 access token은 opaque 값으로 전달하며 JWT를 임의 해석해 계정·권한 확인을 대신하지 않는다. [Device-code 응답 계약](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code), [MSAL ClientApplication의 exclude_scopes](https://learn.microsoft.com/en-us/python/api/msal/msal.application.clientapplication?view=msal-py-latest)

**비공개 캐시와 Linux 실행 조건.** 설치 wrapper의 `/home/juke/superclaude-env/bin/python3`는 조사 시 Python 3.12.3, `msal` 1.34.0, `msal-extensions` 1.3.1, `secretstorage` 3.5.0, `jeepney` 0.9.0, `keyring` 25.7.0을 사용한다. `libsecret-1`과 D-Bus 주소 환경변수는 존재하지만 해당 Python의 `gi`/PyGObject는 없다. 이는 버전·라이브러리 존재 확인이며, 실제 keyring 상태·암호화 보호·사용자 systemd 접근·재부팅 후 이용 가능성은 확인하지 않았다.

MSAL 기본 캐시는 메모리에만 있으므로 지속 저장이 필요하다. `PersistedTokenCache`는 저장 시 파일 잠금과 재읽기를 제공한다. 그러나 표준 `LibsecretPersistence` 1.3.1은 `gi`에 의존하고, 생성자에서 시험 secret을 저장·읽기·삭제하는 `trial_run()`을 호출한다. 단순 status/doctor에서 이를 생성하면 읽기 전용 점검이 아니며, 잠금 사전 확인만으로 이후 UI 발생 가능성까지 제거하지 못한다. 따라서 이번 구현에는 `PersistedTokenCache`를 유지하면서 OS Secret Service에 접근하는 좁은 `BasePersistence` adapter를 사용한다. 새로운 암호 알고리즘이나 평문 저장 fallback은 도입하지 않는다. [MSAL Extensions 공식 사용법](https://github.com/AzureAD/microsoft-authentication-extensions-for-python), [1.3.1 persistence 구현](https://raw.githubusercontent.com/AzureAD/microsoft-authentication-extensions-for-python/1.3.1/msal_extensions/persistence.py), [1.3.1 libsecret 구현](https://raw.githubusercontent.com/AzureAD/microsoft-authentication-extensions-for-python/1.3.1/msal_extensions/libsecret.py)

- 새 `<private_home>/auth/teams/<앱·tenant·교수 ID 조합 해시>/`에 signal·lock·계정 binding만 둔다. 디렉터리 0700, binding·signal 파일 0600과 symlink 거절을 적용한다. MSAL의 일시적인 lock 파일은 PID·명령 정보만 담으며 0700 디렉터리 안에서 다른 프로세스의 기본 생성 권한을 허용하되 소유자·일반 파일·단일 hardlink 검사는 유지한다. token payload는 해당 앱 namespace와 정확한 attributes로 선택한 Secret Service item에만 저장하며 다른 앱 cache를 검색하거나 가져오지 않는다. 토큰·device_code·cache serialization을 config, SQLite, 로그, stdout에 기록하지 않는다. 최초 로그인 안내의 짧은 user code와 verification URI만 대화형 사용자 화면에 표시하고 journal에 보내지 않는다.
- SecretStorage의 `Collection(...)`로 기존 collection만 연다. `get_default_collection()`은 없을 때 자동 생성하고, `create_item()`은 필요한 prompt를 실행하므로 사용하지 않는다. `Unlock`, `Prompt`, `exec_prompt`, 자동 collection 생성과 임시 session collection fallback을 금지한다. 암호화된 Secret Service session이 아니면 실패한다. raw `CreateItem`에서 prompt 경로가 반환되거나, 사전 확인 후 collection이 잠겨 `IsLocked`가 발생하면 기록을 저장하지 못한 상태로 실패한다. [SecretStorage 3.5.0 구현](https://raw.githubusercontent.com/mitya57/secretstorage/3.5.0/secretstorage/collection.py)
- Secret Service의 session 암호화는 **전송 암호화**다. 이것만으로 디스크의 암호화 보호 상태가 검증됐다고 표시하지 않는다. 실제 OS keyring의 보호·잠금 설정은 운영 전 별도 확인 조건이다. attributes와 label은 비밀 저장 영역이 아니므로 토큰을 넣지 않는다. [Secret Service 명세의 저장·전송·잠금·prompt 계약](https://specifications.freedesktop.org/secret-service/latest-single/)
- adapter는 기존 token payload의 접근·복호화 실패를 ‘캐시 없음’으로 바꾸지 않는다. 새 값의 저장 성공 뒤에만 전용 signal의 수정 시각을 갱신하고, `PersistedTokenCache`의 잠금·재읽기 계약에 맞춰 동시 CLI/timer 갱신을 검증한다. `deserialize()`만 호출하고 디스크 저장도 완료됐다고 가정하지 않는다. [PersistedTokenCache 1.3.1 구현](https://raw.githubusercontent.com/AzureAD/microsoft-authentication-extensions-for-python/1.3.1/msal_extensions/token_cache.py)

**연결 코드와 실패 상태.** `TeamsGraphClient`의 고정 문자열 의존을 요청별 token provider로 바꾸되 Graph host 제한·redirect 금지·읽기 전용 요청·기존 페이지 검사를 유지한다. 설정은 기존 네 Teams 범위 필드를 유지하고 `teams_auth`에 명시적 mode와 전용 client ID를 추가한다. authority와 교수 binding은 기존 tenant·단일 교수 ID에서 유도한다. MSAL mode가 선택되면 인증 실패를 env token이나 다른 cache로 자동 대체하지 않는다. 앱·tenant·계정·수집 scope 변경 시 기존 체크포인트를 다른 계정의 범위로 재사용하지 않는다.

정기 작업은 정확히 binding된 계정으로 `acquire_token_silent_with_error`만 호출한다. 유효 access token이 없으면 MSAL이 cache의 refresh token으로 갱신한다. cache miss, `interaction_required`·재인증을 요구하는 `invalid_grant`, 권한·동의 부족, 저장소 잠김·불가, 네트워크 실패를 각각 안전한 오류 코드로 구분한다. timer에서 device-code 로그인이나 브라우저를 자동 시작하지 않는다. 성공한 인증 갱신과 실제 페이지 동기화 완료를 별도로 기록한다. [MSAL silent acquisition](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens)

Graph 401의 `insufficient_claims`는 일반 네트워크 재시도와 다르다. 고정된 Graph 응답의 claims challenge를 검증해 MSAL에 전달하거나 `reauth_required`로 중단하며, 범위를 늘리거나 임의 authority로 이동하지 않는다. 같은 요청의 강제 silent refresh·재전송은 한 번으로 제한한다. MFA·sign-in frequency, 계정·세션 회수, Conditional Access 변경 때문에 나중에 사람의 로그인이 다시 필요할 수 있다. 특히 device-code 차단 정책은 후속 refresh에도 적용될 수 있으므로 최초 로그인 한 번으로 영구 무인 실행을 보장하지 않는다. [Claims challenge](https://learn.microsoft.com/en-us/entra/identity-platform/claims-challenge), [MSAL device-flow claims 인자](https://learn.microsoft.com/en-us/python/api/msal/msal.application.publicclientapplication?view=msal-py-latest), [Refresh token 회수](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens), [Authentication-flow Conditional Access](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-authentication-flows), [Session lifetime 정책](https://learn.microsoft.com/en-us/entra/identity/conditional-access/howto-conditional-access-session-lifetime)

**완료 판정.** 합성 테스트로 계정 불일치, scope 누락, expired access token의 silent 갱신, cache miss, 회수·claims challenge, 잠금 race, prompt 반환, 암호화 session 협상 실패, 저장 실패·동시 갱신, token 문자열의 출력 누출 여부를 먼저 확인한다. 실제 운영 준비는 전용 앱·동의·초기 사용자 로그인, 사용자 systemd 문맥에서의 저장소 접근, access token 만료 후 silent 갱신을 각각 별도 증거로 확인해야 한다. 이번 조사는 해당 외부·인증 동작을 실행하지 않았다.

## 7. 수집 상태와 검증 시나리오

아래는 이 구현에 필요한 실제 실패 시나리오다. 수집 함수와 같은 계산을 반복하는 형식적 테스트보다, 누락·오인·정보 잔존을 일으키는 입력을 고정한다.

| 사례 | 기대 결과 |
|---|---|
| 초기 목록 수집 도중 Drive 수정·Gmail 새 메일 도착 | 사전 경계부터 catch-up하여 누락 없음 |
| 저장 직전·직후 프로세스 종료, 같은 page 재전달 | source 중복 없음, 커서가 미처리 페이지를 건너뛰지 않음 |
| scope allowlist 또는 인증 계정 변경 | 이전 범위 커서 재사용 거부, 새 generation 생성 |
| Drive removed event에 file 객체 없음 | 예외 없이 가용성 철회와 파생 근거 비활성화 |
| Drive 소유자=교수, 본문은 공동 저술 | 직접 교수 발언 자동 승격 없음 |
| 교수와 동일 표시명의 댓글 작성자 | displayName만으로 identity 확정 없음 |
| Docs 두 번째 tab·중첩 child tab에 핵심 원칙 | 모든 tab의 원문과 locator 수집 |
| Drive 댓글 답글만 수정·삭제 | 본문 hash 불변이어도 댓글 갱신, 삭제 내용 재사용 안 함 |
| Gmail 별칭 발신·과거 lab member·그룹 주소 | 검증된 주소 및 당시 재적 근거로 분류 |
| Gmail 다중 multipart·인라인 회신·한국어 인용 | 직접 발언과 인용문 분리; 불확실 구간 보류 |
| 같은 교수 문장이 20회 인용됨 | 독립 근거 수 20으로 부풀리지 않음 |
| Gmail history 404 | full sync 필요 상태와 재수집; 조용히 최신 ID로 이동하지 않음 |
| Gmail 휴지통 이동 vs 영구 삭제 | 라벨 변경과 messagesDeleted를 구별 |
| 알림 없음·중복·역순·누락 | 주기 polling으로 수렴, 알림 자체로 cursor 확정 안 함 |
| Teams 201개 답글·root 두 번째 페이지 | 두 종류 nextLink 모두 처리 |
| 두 Teams 채팅에서 message ID 동일 | 복합키로 별도 원문 유지 |
| Teams 반응만 추가됨 | 동기화는 반영하되 새 철학 발언 생성 없음 |
| Teams 9개월 전 중요한 chat | delta 완료만으로 전체 이력 완료 보고하지 않음 |
| Teams 날짜 filter에 orderby 누락 | 요청 검증에서 실패; 서버의 무시 동작에 맡기지 않음 |
| Teams subscriptionRemoved·권한 회수 | 구독 복구와 공백 재대조; 접근 상실 근거 사용 중지 |
| 내용 삭제 후 검색·적용 프롬프트 생성 | 삭제 원문·파생 chunk가 결과에 나타나지 않음 |
| 원문 안에 ‘이전 규칙 무시’ 또는 실행 명령 | 철학 후보 데이터로만 취급; 도구 실행·상위 지시 승격 없음 |

운영 지표는 `sources_seen`, `sources_fetched`, `sources_failed`, `direct_authorship_verified`, `context_only`, `unresolved_authorship`, `pages_pending`, `threads_incomplete`, `last_source_timestamp`, `last_successful_sync`, `revocations_pending`이다. 공급자별 범위와 마지막 성공 시각을 함께 보고한다. 이력 빈 응답은 새 변경이 없다는 뜻일 뿐 전체 과거 이력 검증을 대신하지 않는다.

## 8. 구현 우선순위와 남은 확인

1. 기존 Google·Teams 읽기 도구와 로컬 보관 자료를 재사용하되 source key·작성자 근거·수집 시각을 잃지 않는 adapter를 만든다. 기존 archive의 freshness와 live identity는 별도 검증한다.
2. 합성 자료로 source ledger, cursor transaction, scope 변경, 삭제 전파, 인용 분리부터 통과시킨다.
3. 실제 계정의 좁은 교수 발언 canary를 읽고 위 판정을 원문과 대조한다. 이 단계의 수집 완료를 전체 Drive·Gmail·Teams 완료로 표현하지 않는다.
4. 각 공급자의 bootstrap 범위를 열거하고 전수 수집을 진행한다. 실패·미지원·작성자 불명 건을 완료 보고에 남긴다.
5. 주기 갱신을 실행하고 실제 한 차례의 새 발언·수정·삭제를 끝까지 확인한다. 그 다음 필요 시 webhook을 추가한다.

공식 문서만으로는 현재 사용자 계정의 OAuth grant, Teams tenant 동의, 과거 채널·chat 열거 범위, 교수의 모든 alias, 연구실 멤버의 시기별 주소를 확인할 수 없다. 이 값은 API 계약과 분리해 live metadata 또는 사용자 확인으로 채운다. 이 보고서의 API 조사 범위는 완료했으며, 인증·비공개 원천 전수 수집·동작 검증은 후속 실행에서 확인할 사항이다.
