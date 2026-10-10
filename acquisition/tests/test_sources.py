from pathlib import Path
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.sources import Sources, SourceError, DownloadCancelled, normalize_soulseek, youtube_url


class SourceTests(unittest.TestCase):
    def test_normalization_excludes_locked_non_audio_and_groups_albums(self):
        data = {'responses': [{'username': 'peer', 'hasFreeUploadSlot': True, 'queueLength': 4,
                'files': [{'filename': 'Music\\Album\\01 Song.flac', 'size': 99, 'bitRate': 900},
                          {'filename': 'Music\\Album\\02 Song.mp3', 'size': 50, 'length': 180},
                          {'filename': 'Music\\Album\\cover.jpg', 'size': 20}],
                'lockedFiles': [{'filename': 'Music\\Private.mp3', 'size': 42}]}]}
        tracks = normalize_soulseek(data)
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0]['bitrate'], 900)
        album = normalize_soulseek(data, 'album')[0]
        self.assertEqual(album['file_count'], 2)
        self.assertEqual(album['size'], 149)
        self.assertFalse(album['folder_complete'])

    def test_urls_reject_private_hosts_credentials_and_deceptive_domains(self):
        for url in ['http://127.0.0.1/a', 'http://10.0.0.1/a', 'file:///etc/passwd',
                    'https://youtube.com.evil.test/a', 'https://youtube.com@127.0.0.1/',
                    'https://youtube.com:8080/a', 'http://[::1]/', 'https://bandcamp.com.evil.test/a',
                    'https://bandcamp.com@localhost/a', 'https://youtube.com:notaport/a', 'http://[bad/']:
            with self.subTest(url=url), self.assertRaises(SourceError):
                youtube_url(url)
        for url in ['https://youtu.be/abc', 'https://soundcloud.com/artist/track',
                    'https://artist.bandcamp.com/album/album', 'https://vimeo.com/123']:
            self.assertEqual(youtube_url(url), url)

    def test_completed_paths_reject_traversal_and_symlinks(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as outside:
            sources = Sources({'slskd_download_root': root})
            with self.assertRaises(SourceError):
                sources._completed_file({'filename': 'Album\\..\\secret.mp3', 'size': 3})
            p = Path(outside) / 'song.mp3'
            p.write_bytes(b'abc')
            (Path(root) / 'Album').symlink_to(outside)
            with self.assertRaises(SourceError):
                sources._completed_file({'filename': 'Album\\song.mp3', 'size': 3})

    def test_download_reuses_completed_transfer_without_enqueue(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as destination:
            p = Path(root) / 'Album' / 'Song.mp3'
            p.parent.mkdir()
            p.write_bytes(b'audio')
            sources = Sources({'slskd_download_root': root})
            transfer = {'id': 't1', 'filename': 'Music\\Album\\Song.mp3', 'size': 5,
                        'state': 'Completed, Succeeded', 'bytesTransferred': 5}
            candidate = {'source': 'soulseek', 'username': 'peer', 'kind': 'track',
                         'files': [{'filename': transfer['filename'], 'size': 5}]}
            events = []
            with patch.object(sources, '_transfers', return_value=[transfer]), patch.object(sources, '_slskd') as api:
                files = sources.download(candidate, Path(destination), events.append, lambda: False)
                api.assert_not_called()
            self.assertEqual(files[0].read_bytes(), b'audio')
            self.assertTrue(p.exists())
            self.assertEqual(candidate['transfer_ids'], ['t1'])
            self.assertEqual(events[-1]['percent'], 100)

    def test_search_source_failure_is_visible_and_other_results_survive(self):
        sources = Sources({})
        with patch.object(sources, '_search_soulseek', side_effect=SourceError('Soulseek offline.')), \
             patch.object(sources, '_search_youtube', return_value=[{'id': 'x'}]):
            self.assertEqual(sources.search('track'), [{'id': 'x'}])
            self.assertEqual(sources.last_errors, {'soulseek': 'Soulseek offline.'})
        with patch.object(sources, '_search_soulseek', side_effect=SourceError('offline')):
            with self.assertRaises(SourceError):
                sources.search('track', 'soulseek')

    def test_cancelled_download_does_not_enqueue(self):
        sources = Sources({})
        with TemporaryDirectory() as destination, patch.object(sources, '_slskd') as api:
            with self.assertRaises(DownloadCancelled):
                sources.download({'source': 'soulseek'}, destination, lambda _: None, lambda: True)
            api.assert_not_called()

    def test_album_hydrates_full_folder_before_enqueue(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as destination:
            sources = Sources({'slskd_download_root': root})
            candidate = {'source': 'soulseek', 'username': 'peer', 'kind': 'album', 'directory': 'Music\\Album',
                         'files': [{'filename': 'Music\\Album\\01.mp3', 'size': 5}]}
            transfers = []
            for n in ['01', '02']:
                p = Path(root) / 'Album' / f'{n}.mp3'
                p.parent.mkdir(exist_ok=True)
                p.write_bytes(b'audio')
                transfers.append({'id': n, 'filename': f'Music\\Album\\{n}.mp3', 'size': 5,
                                  'state': 'Completed, Succeeded', 'bytesTransferred': 5})
            folder = [{'name': 'Music\\Album', 'files': [{'filename': f'{n}.mp3', 'size': 5} for n in ['01', '02']]}]
            with patch.object(sources, '_slskd', return_value=folder), patch.object(sources, '_transfers', return_value=transfers):
                files = sources.download(candidate, destination, lambda _: None, lambda: False)
            self.assertEqual(len(files), 2)
            self.assertTrue(candidate['folder_complete'])

    def test_album_folder_from_search_results_when_the_peer_lists_no_folders(self):
        for listing, files, ok in [([], 2, True), ([], 1, False),
                                   ([{'name': 'Music\\Other', 'files': [{'filename': 'x.mp3', 'size': 5}]}], 2, False)]:
            with self.subTest(listing=listing, files=files), TemporaryDirectory() as root, TemporaryDirectory() as destination:
                sources = Sources({'slskd_download_root': root})
                names = [f'{n:02d}.mp3' for n in range(files)]
                candidate = {'source': 'soulseek', 'username': 'peer', 'kind': 'album', 'directory': 'Music\\Album',
                             'files': [{'filename': f'Music\\Album\\{n}', 'size': 5} for n in names]}
                transfers = []
                for n in names:
                    p = Path(root) / 'Album' / n
                    p.parent.mkdir(exist_ok=True)
                    p.write_bytes(b'audio')
                    transfers.append({'id': n, 'filename': f'Music\\Album\\{n}', 'size': 5, 'state': 'Completed, Succeeded'})
                with patch.object(sources, '_slskd', return_value=listing), patch.object(sources, '_transfers', return_value=transfers):
                    if ok:
                        self.assertEqual(len(sources.download(candidate, destination, lambda _: None, lambda: False)), files)
                        self.assertFalse(candidate.get('folder_complete'))
                    else:
                        with self.assertRaisesRegex(SourceError, 'Could not confirm'):
                            sources.download(candidate, destination, lambda _: None, lambda: False)

    def test_playlist_limit_and_unavailable_entries_fail_explicitly(self):
        sources = Sources({'max_playlist_files': 2})
        for info in [
            {'entries': [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}]},
            {'entries': [{'id': 'a'}, None]},
            {'entries': [{'id': 'a'}], 'playlist_count': 2},
        ]:
            with self.subTest(info=info), self.assertRaises(SourceError):
                sources._validate_playlist(info, {'kind': 'album'})
        with self.assertRaises(SourceError):
            sources._validate_playlist({'entries': [{'id': 'a'}]}, {'kind': 'track'})
        self.assertEqual(len(sources._validate_playlist({'entries': [{'id': 'a'}, {'id': 'b'}]}, {'kind': 'album'})), 2)
        self.assertNotIn('playlistend', sources._youtube_options())

    def test_youtube_premium_cookies_are_used_only_when_the_file_exists(self):
        with TemporaryDirectory() as tmp:
            cookies = Path(tmp) / 'cookies.txt'
            sources = Sources({'youtube_cookies_file': str(cookies)})
            self.assertNotIn('cookiefile', sources._youtube_options())
            cookies.write_text('# Netscape HTTP Cookie File\n')
            self.assertEqual(sources._youtube_options()['cookiefile'], str(cookies))

    def test_cancel_only_owned_remote_transfers(self):
        sources = Sources({})
        candidate = {'source': 'soulseek', 'username': 'peer', 'transfer_ids': ['shared', 'own'], 'owned_transfer_ids': ['own']}
        with patch.object(sources, '_slskd') as api:
            sources.cancel(candidate)
        api.assert_called_once_with('DELETE', '/transfers/downloads/peer/own', params={'remove': 'false'})

    def test_ytdlp_preserves_source_identity_and_checks_complete_audio(self):
        from unittest.mock import MagicMock
        with TemporaryDirectory() as destination:
            sources = Sources({})
            candidate = {'source': 'youtube', 'kind': 'track', 'url': 'https://youtu.be/abc',
                         'title': 'Search label', 'requested_title': 'Requested song'}
            info = {'id': 'abc', 'title': 'Actual uploaded title', 'track': 'Real song',
                    'artist': 'Real artist', 'album': 'Real album', 'uploader': 'Uploader'}
            def extract(url, download=False):
                if download:
                    (Path(destination) / 'abc.opus').write_bytes(b'audio')
                return info
            downloader = MagicMock()
            downloader.extract_info.side_effect = extract
            context = MagicMock()
            context.__enter__.return_value = downloader
            with patch('app.sources.yt_dlp.YoutubeDL', return_value=context) as ytdl:
                events = []
                files = sources.download(candidate, destination, events.append, lambda: False)
            download_options = ytdl.call_args_list[-1].args[0]
            self.assertTrue(download_options['writethumbnail'])
            self.assertEqual([p['key'] for p in download_options['postprocessors']][-1], 'EmbedThumbnail')
            self.assertEqual(files[0].name, 'abc.opus')
            self.assertEqual(candidate['source_title'], 'Real song')
            self.assertEqual(candidate['requested_title'], 'Requested song')
            self.assertEqual(candidate['artist'], 'Real artist')
            self.assertEqual(candidate['source_metadata']['title'], 'Actual uploaded title')
            self.assertEqual(candidate['source_files'][0]['track'], 'Real song')

    def test_playlist_missing_final_audio_never_returns_partial_album(self):
        from unittest.mock import MagicMock
        with TemporaryDirectory() as destination:
            sources = Sources({})
            info = {'id': 'playlist', 'title': 'Album', 'entries': [{'id': 'a'}, {'id': 'b'}]}
            downloader = MagicMock()
            downloader.extract_info.return_value = info
            context = MagicMock()
            context.__enter__.return_value = downloader
            (Path(destination) / 'a.opus').write_bytes(b'audio')
            with patch('app.sources.yt_dlp.YoutubeDL', return_value=context), self.assertRaises(SourceError):
                sources.download({'source': 'youtube', 'kind': 'album', 'url': 'https://youtube.com/playlist?list=x'},
                                 destination, lambda _: None, lambda: False)

    def test_ytdlp_does_not_promote_uploader_or_categories_to_music_tags(self):
        from unittest.mock import MagicMock
        with TemporaryDirectory() as destination:
            sources = Sources({})
            info = {'id': 'abc', 'title': 'Uploaded title', 'uploader': 'Uploader', 'categories': ['Music']}
            captured = []
            def context(options):
                captured.append(options)
                downloader = MagicMock()
                def extract(url, download=False):
                    if download:
                        options['postprocessor_hooks'][0]({'status': 'started', 'info_dict': info})
                        (Path(destination) / 'abc.opus').write_bytes(b'audio')
                    return info
                downloader.extract_info.side_effect = extract
                result = MagicMock()
                result.__enter__.return_value = downloader
                return result
            with patch('app.sources.yt_dlp.YoutubeDL', side_effect=context):
                sources.download({'source': 'youtube', 'kind': 'track', 'url': 'https://youtu.be/abc'},
                                 destination, lambda _: None, lambda: False)
            self.assertEqual(info['meta_artist'], '')
            self.assertEqual(info['meta_genre'], '')
            self.assertEqual(info['uploader'], 'Uploader')


    def test_retry_reenqueues_terminal_attempt_and_keeps_partial(self):
        for state in ['Completed, TimedOut', 'Errored', 'Rejected', 'Cancelled', 272]:
            with self.subTest(state=state), TemporaryDirectory() as root, TemporaryDirectory() as destination:
                sources = Sources({'slskd_download_root': root})
                complete = Path(root) / 'Album' / 'Song.mp3'
                complete.parent.mkdir()
                complete.write_bytes(b'audio')
                partial = Path(root) / 'unfinished.part'
                partial.write_bytes(b'partial')
                old = {'id': 'old', 'filename': 'Music\\Album\\Song.mp3', 'size': 5, 'state': state}
                new = {**old, 'id': 'new', 'state': 'Completed, Succeeded', 'bytesTransferred': 5}
                candidate = {'source': 'soulseek', 'username': 'peer', 'kind': 'track',
                             'files': [{'filename': old['filename'], 'size': 5}]}
                with patch.object(sources, '_transfers', side_effect=[[old], [old, new]]), \
                     patch.object(sources, '_slskd', return_value={'enqueued': [new], 'failed': []}) as api:
                    files = sources.download(candidate, destination, lambda _: None, lambda: False)
                api.assert_called_once_with('POST', '/transfers/downloads/peer', json=candidate['files'])
                self.assertEqual(files[0].read_bytes(), b'audio')
                self.assertEqual(partial.read_bytes(), b'partial')
                self.assertEqual(candidate['owned_transfer_ids'], ['new'])

    def test_remote_queue_progress_and_timeout_reuse_existing_transfer(self):
        sources = Sources({'download_stall_seconds': 0.02, 'slskd_poll_seconds': 0.001})
        queued = {'id': 'queued', 'filename': 'Music\\Song.mp3', 'size': 5,
                  'state': 'Queued, Remotely', 'placeInQueue': 7, 'bytesTransferred': 2}
        old = {**queued, 'id': 'old', 'state': 'Completed, Errored'}
        candidate = {'source': 'soulseek', 'username': 'peer', 'files': [{'filename': queued['filename'], 'size': 5}]}
        events = []
        with TemporaryDirectory() as destination, patch.object(sources, '_transfers', return_value=[queued, old]), \
             patch.object(sources, '_slskd') as api:
            with self.assertRaisesRegex(SourceError, 'peer stopped sending.*trying another copy'):
                sources.download(candidate, destination, events.append, lambda: False)
            api.assert_not_called()
        self.assertTrue(any('position 7' in event['message'] for event in events))
        self.assertEqual(candidate['transfer_ids'], ['queued'])

    def test_transfer_failure_describes_peer_and_retained_partials(self):
        for state, detail, expected in [
            ('Completed, Errored', 'User peer appears to be offline', 'offline or unavailable'),
            ('TimedOut', '', 'did not respond in time'),
            ('Completed, Rejected', 'Too many failed transfers today', 'download quota'),
            ('Completed, Rejected', 'Too many files this week', 'download quota'),
            ('Completed, Rejected', 'Verification required', 'human check.*private message'),
            ('Completed, Rejected', 'Banned', 'banned this account'),
            ('Completed, Errored', 'Download failed to enqueue remotely after hard time limit of 180 secs', 'never accepted'),
            ('Aborted', 'connection reset by peer', 'interrupted'),
            ('Completed, Errored', 'File not shared', 'no longer shares this file'),
        ]:
            with self.subTest(state=state), TemporaryDirectory() as destination:
                sources = Sources({})
                old = {'id': 'old', 'filename': 'Music\\Song.mp3', 'size': 5, 'state': 'Completed, Errored'}
                attempt = {**old, 'id': 'attempt', 'state': state, 'exception': detail}
                candidate = {'source': 'soulseek', 'username': 'peer', 'files': [{'filename': old['filename'], 'size': 5}]}
                with patch.object(sources, '_transfers', side_effect=[[old], [attempt]]), \
                     patch.object(sources, '_slskd', return_value={'enqueued': [attempt], 'failed': []}):
                    with self.assertRaisesRegex(SourceError, expected) as error:
                        sources.download(candidate, destination, lambda _: None, lambda: False)
                self.assertIn('Partial files are retained', str(error.exception))

    def test_failed_enqueue_returns_remote_reason_and_tolerates_concurrent_queue(self):
        for concurrent in [False, True]:
            with self.subTest(concurrent=concurrent), TemporaryDirectory() as root, TemporaryDirectory() as destination:
                sources = Sources({'slskd_download_root': root})
                path = Path(root) / 'Album' / 'Song.mp3'
                path.parent.mkdir()
                path.write_bytes(b'audio')
                transfer = {'id': 'shared', 'filename': 'Music\\Album\\Song.mp3', 'size': 5, 'state': 'Completed, Succeeded'}
                candidate = {'source': 'soulseek', 'username': 'peer', 'files': [{'filename': transfer['filename'], 'size': 5}]}
                response = {'enqueued': [], 'failed': [{'filename': transfer['filename'], 'message': 'User peer appears to be offline'}]}
                lists = [[], [transfer], [transfer]] if concurrent else [[], []]
                with patch.object(sources, '_transfers', side_effect=lists), patch.object(sources, '_slskd', return_value=response) as api:
                    if concurrent:
                        files = sources.download(candidate, destination, lambda _: None, lambda: False)
                        self.assertEqual(files[0].read_bytes(), b'audio')
                        self.assertEqual(candidate['owned_transfer_ids'], [])
                    else:
                        with self.assertRaisesRegex(SourceError, 'offline or unavailable'):
                            sources.download(candidate, destination, lambda _: None, lambda: False)
                    self.assertEqual(api.call_count, 1)

    def test_http_enqueue_error_exposes_offline_reason_and_busy_status(self):
        import httpx
        for status, detail, expected in [(500, 'User peer appears to be offline', 'offline or unavailable'),
                                          (429, 'Only one concurrent operation is permitted', 'Soulseek is busy')]:
            with self.subTest(status=status):
                request = httpx.Request('POST', 'http://slskd/api/v0/transfers/downloads/peer')
                response = httpx.Response(status, json=detail, request=request)
                with patch('app.sources.httpx.request', return_value=response), self.assertRaisesRegex(SourceError, expected):
                    Sources({})._slskd('POST', '/transfers/downloads/peer', json=[])

    def test_a_peer_that_times_out_is_that_peers_failure_not_an_outage(self):
        import httpx
        from app.sources import SoulseekUnavailable
        detail = 'The wait timed out after 5000 milliseconds'
        for path in ('/users/some%20peer/directory', '/transfers/downloads/some%20peer'):
            with self.subTest(path=path):
                response = httpx.Response(500, json=detail, request=httpx.Request('POST', 'http://slskd/api/v0' + path))
                with patch('app.sources.httpx.request', return_value=response):
                    with self.assertRaisesRegex(SourceError, 'some peer did not respond in time') as caught:
                        Sources({})._slskd('POST', path, json={})
                    self.assertNotIsInstance(caught.exception, SoulseekUnavailable)
        response = httpx.Response(500, json=detail, request=httpx.Request('GET', 'http://slskd/api/v0/transfers/downloads'))
        with patch('app.sources.httpx.request', return_value=response), self.assertRaises(SoulseekUnavailable):
            Sources({})._slskd('GET', '/transfers/downloads')


    def test_retry_reenqueues_stale_completed_record_with_missing_file(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as destination:
            sources = Sources({'slskd_download_root': root})
            old = {'id': 'old', 'filename': 'Music\\Album\\Song.mp3', 'size': 5, 'state': 'Completed, Succeeded'}
            new = {**old, 'id': 'new', 'bytesTransferred': 5}
            candidate = {'source': 'soulseek', 'username': 'peer', 'files': [{'filename': old['filename'], 'size': 5}]}
            reads = 0
            def transfers(username):
                nonlocal reads
                reads += 1
                if reads == 1:
                    return [old]
                source = Path(root) / 'Album' / 'Song.mp3'
                source.parent.mkdir(exist_ok=True)
                source.write_bytes(b'audio')
                return [new, old]
            with patch.object(sources, '_transfers', side_effect=transfers), \
                 patch.object(sources, '_slskd', return_value={'enqueued': [new], 'failed': []}) as api:
                files = sources.download(candidate, destination, lambda _: None, lambda: False)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(candidate['transfer_ids'], ['new'])
            self.assertEqual(files[0].read_bytes(), b'audio')


if __name__ == '__main__':
    unittest.main()


class SoulseekSearchTests(unittest.TestCase):
    def test_queries_keep_only_words_peers_can_match(self):
        from app.sources import soulseek_query
        self.assertEqual(soulseek_query('Khadija Al Hanafi ¡OK!'), 'khadija al hanafi ok')
        self.assertEqual(soulseek_query("Normal Pleasure MELBOURNE'S DEAD"), 'normal pleasure melbourne dead')
        self.assertEqual(soulseek_query("I Lay Down My Life For You (DIrector's Cut)"), 'lay down my life for you')
        self.assertEqual(soulseek_query('Danny Ocean Me Rehúso feat. Someone'), 'danny ocean me rehúso')
        self.assertEqual(soulseek_query('clipping. - Dead Channel Sky [2025]'), 'clipping dead channel sky')
        self.assertEqual(soulseek_query('Jawnino 40'), 'jawnino 40')
        # A name the server drops searches for is left out; "-word" would be an exclusion.
        self.assertEqual(soulseek_query('JPEGMAFIA - Scaring the Hoes'), 'scaring the hoes')

    def test_limiter_spaces_searches_and_keeps_the_window_rate(self):
        from app.sources import SearchLimiter
        now = [0.0]
        waits = []

        def sleep(seconds):
            waits.append(round(seconds, 2))
            now[0] += seconds

        limiter = SearchLimiter(per_window=3, window=100, spacing=5, clock=lambda: now[0], sleep=sleep)
        for _ in range(4):
            with limiter:
                now[0] += 1   # the search itself
        # 5 s between starts; the 4th waits for the first to leave the 100 s window.
        self.assertEqual(waits, [4.0, 4.0, 89.0])

    def test_responses_are_read_after_the_search_ends_and_then_deleted(self):
        from app import sources as module
        calls, states = [], iter([{}, {}, {'endedAt': '2026-10-06T20:00:00Z'}])

        def api(method, path, **kwargs):
            calls.append((method, path.split('/')[-1] if path.endswith('responses') else method))
            if method == 'GET' and path.endswith('/responses'):
                return [{'username': 'peer', 'files': [{'filename': 'Music\\Album\\01 a.flac', 'size': 1}]}]
            if method == 'GET':
                return next(states)
            return None

        source = Sources({'slskd_search_seconds': 1})
        with patch.object(source, '_slskd', side_effect=api), patch.object(module.time, 'sleep'), \
             patch.object(module, 'soulseek_searches', module.SearchLimiter(sleep=lambda s: None)):
            results = source._search_soulseek('Album', 'album')
        self.assertEqual(len(results), 1)
        self.assertEqual([c[0] for c in calls], ['POST', 'GET', 'GET', 'GET', 'GET', 'DELETE'])
        self.assertEqual(calls[-2][1], 'responses')

    def test_unfinished_search_is_cancelled_before_reading(self):
        from app import sources as module
        calls = []
        clock = [0.0]

        def api(method, path, **kwargs):
            calls.append(method)
            if method == 'PUT':
                return None
            if method == 'GET' and path.endswith('/responses'):
                return []
            return {'endedAt': '2026'} if 'PUT' in calls else {}

        def sleep(seconds):
            clock[0] += 30

        source = Sources({'slskd_search_seconds': 1})
        with patch.object(source, '_slskd', side_effect=api), patch.object(module.time, 'sleep', sleep), \
             patch.object(module.time, 'monotonic', lambda: clock[0]), \
             patch.object(module, 'soulseek_searches', module.SearchLimiter(sleep=lambda s: None, clock=lambda: clock[0])):
            source._search_soulseek('Album', 'album')
        self.assertIn('PUT', calls)
        self.assertLess(calls.index('PUT'), calls.index('DELETE'))

    def test_disc_folders_merge_into_one_album(self):
        data = [{'username': 'peer', 'files': [
            {'filename': 'Music\\Artist - Album\\CD1\\01 a.flac', 'size': 1},
            {'filename': 'Music\\Artist - Album\\Disc 2\\01 b.mp3', 'size': 1}]}]
        albums = normalize_soulseek(data, 'album')
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]['leaf_folder'], 'Artist - Album')
        self.assertEqual(albums[0]['file_count'], 2)
        self.assertEqual(albums[0]['formats'], {'flac': 1, 'mp3': 1})
        self.assertTrue(albums[0]['mixed_formats'])


class QueueLimitedPeer:
    """An uploader that accepts at most `limit` queued files per user and refuses the rest with
    "Too many files"; each poll finishes the accepted transfers."""

    def __init__(self, root, limit):
        self.root, self.limit, self.transfers, self.posts = Path(root), limit, [], 0

    def slskd(self, method, path, json=None, **kwargs):
        if method != 'POST':
            return None   # cancelling a transfer
        self.posts += 1
        for file in json:
            active = sum(t['state'] == 'Queued, Remotely' for t in self.transfers)
            accepted = active < self.limit
            self.transfers.append({'id': f't{len(self.transfers)}', 'filename': file['filename'], 'size': file['size'],
                                   'state': 'Queued, Remotely' if accepted else 'Completed, Rejected',
                                   'exception': None if accepted else 'Transfer rejected: Too many files'})
        return {'enqueued': [t for t in self.transfers[-len(json):]], 'failed': []}

    def poll(self, username):
        current = [dict(t) for t in self.transfers]
        for t in self.transfers:
            if t['state'] == 'Queued, Remotely':
                name = t['filename'].split('\\')[-1]
                (self.root / 'Album').mkdir(exist_ok=True)
                (self.root / 'Album' / name).write_bytes(b'x' * t['size'])
                t.update(state='Completed, Succeeded', bytesTransferred=t['size'])
        return current


class QueueLimitTests(unittest.TestCase):
    def candidate(self, count):
        return {'source': 'soulseek', 'username': 'peer', 'kind': 'track',
                'files': [{'filename': f'Music\\Album\\{n:02d}.flac', 'size': 3} for n in range(count)]}

    def test_files_refused_for_the_queue_limit_are_requested_again_in_rounds(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as destination:
            peer = QueueLimitedPeer(root, limit=2)
            sources = Sources({'slskd_download_root': root, 'slskd_poll_seconds': 0})
            candidate, events = self.candidate(5), []
            with patch.object(sources, '_transfers', side_effect=peer.poll), patch.object(sources, '_slskd', side_effect=peer.slskd):
                files = sources.download(candidate, Path(destination), events.append, lambda: False)
            self.assertEqual(len(files), 5)
            self.assertEqual(peer.posts, 3)   # 2 + 2 + 1 files accepted per round
            self.assertTrue(any('limits queued files' in str(event.get('message')) for event in events))
            self.assertEqual(len(candidate['owned_transfer_ids']), 9)

    def test_an_uploader_that_keeps_refusing_is_dropped_when_nothing_arrives(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as destination:
            peer = QueueLimitedPeer(root, limit=0)
            sources = Sources({'slskd_download_root': root, 'slskd_poll_seconds': 0.01, 'download_stall_seconds': 0.1})
            with patch.object(sources, '_transfers', side_effect=peer.poll), patch.object(sources, '_slskd', side_effect=peer.slskd):
                with self.assertRaisesRegex(SourceError, 'did not start sending'):
                    sources.download(self.candidate(3), Path(destination), lambda _: None, lambda: False)

    def test_files_the_uploader_did_not_answer_are_requested_again(self):
        # 2026-10-08: et sent 40 of 42 files while two requests timed out; the job must not fail.
        with TemporaryDirectory() as root, TemporaryDirectory() as destination:
            peer = QueueLimitedPeer(root, limit=99)
            original, asked = peer.slskd, set()
            def slow(method, path, json=None):
                response = original(method, path, json)
                for t in peer.transfers[-len(json):]:
                    if t['filename'].endswith('01.flac') and t['filename'] not in asked:
                        asked.add(t['filename'])
                        t.update(state='Completed, TimedOut', exception='Download failed to enqueue remotely after hard time limit of 180 secs')
                return response
            sources = Sources({'slskd_download_root': root, 'slskd_poll_seconds': 0})
            with patch.object(sources, '_transfers', side_effect=peer.poll), patch.object(sources, '_slskd', side_effect=slow):
                files = sources.download(self.candidate(3), Path(destination), lambda _: None, lambda: False)
            self.assertEqual(len(files), 3)
            self.assertEqual(peer.posts, 2)

    def test_an_uploader_that_answers_nothing_fails_at_once(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as destination:
            peer = QueueLimitedPeer(root, limit=99)
            original = peer.slskd
            def silent(method, path, json=None):
                response = original(method, path, json)
                for t in peer.transfers:
                    t.update(state='Completed, TimedOut', exception='Download failed to enqueue remotely after hard time limit of 180 secs')
                return response
            sources = Sources({'slskd_download_root': root, 'slskd_poll_seconds': 0})
            with patch.object(sources, '_transfers', side_effect=peer.poll), patch.object(sources, '_slskd', side_effect=silent):
                with self.assertRaisesRegex(SourceError, 'never accepted the request'):
                    sources.download(self.candidate(3), Path(destination), lambda _: None, lambda: False)
            self.assertEqual(peer.posts, 1)

    def test_other_refusals_still_fail_at_once(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as destination:
            peer = QueueLimitedPeer(root, limit=0)
            sources = Sources({'slskd_download_root': root, 'slskd_poll_seconds': 0})
            original = peer.slskd
            def banned(method, path, json=None):
                response = original(method, path, json)
                for t in peer.transfers:
                    t['exception'] = 'Transfer rejected: Banned'
                return response
            with patch.object(sources, '_transfers', side_effect=peer.poll), patch.object(sources, '_slskd', side_effect=banned):
                with self.assertRaisesRegex(SourceError, 'banned this account'):
                    sources.download(self.candidate(2), Path(destination), lambda _: None, lambda: False)
            self.assertEqual(peer.posts, 1)

    def test_time_limit_counts_from_the_last_progress(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as destination:
            sources = Sources({'slskd_download_root': root, 'download_stall_seconds': 0.05, 'slskd_poll_seconds': 0.02})
            candidate = self.candidate(1)
            polls = 0
            def transfers(username):
                nonlocal polls
                polls += 1
                transfer = {'id': 't', 'filename': candidate['files'][0]['filename'], 'size': 3,
                            'state': 'InProgress', 'bytesTransferred': polls}
                if polls >= 12:   # about 0.24 s in: well past the limit, but bytes kept arriving
                    (Path(root) / 'Album').mkdir(exist_ok=True)
                    (Path(root) / 'Album' / '00.flac').write_bytes(b'xyz')
                    transfer['state'] = 'Completed, Succeeded'
                return [transfer]
            with patch.object(sources, '_transfers', side_effect=transfers), patch.object(sources, '_slskd') as api:
                files = sources.download(candidate, Path(destination), lambda _: None, lambda: False)
            api.assert_not_called()
            self.assertEqual(files[0].read_bytes(), b'xyz')


class HumanCheckTests(unittest.TestCase):
    def conversation(self, *messages):
        return {'username': 'peer', 'messages': [{'direction': d, 'message': m, 'timestamp': t} for d, m, t in messages]}

    def test_open_answered_and_verified_checks(self):
        from app.sources import human_checks
        now = 1_800_000_000
        iso = lambda seconds_ago: __import__('datetime').datetime.fromtimestamp(now - seconds_ago, __import__('datetime').timezone.utc).isoformat()
        ask = ('In', 'To prove you are a human downloading these files, please type "watermelon" in this chat', iso(7200))
        self.assertEqual(human_checks([self.conversation(ask)], now=now)[0]['status'], 'open')
        answered = human_checks([self.conversation(ask, ('Out', 'watermelon', iso(60)))], now=now)
        self.assertEqual(answered[0]['status'], 'answered')
        self.assertEqual(human_checks([self.conversation(ask, ('Out', 'watermelon', iso(7000)))], now=now), [])
        self.assertEqual(human_checks([self.conversation(ask, ('In', 'ProveIt: You are verified.', iso(30)))], now=now), [])
        self.assertEqual(human_checks([self.conversation(('In', 'thanks for sharing!', iso(10)))], now=now), [])
        verified_then_link = self.conversation(ask, ('In', 'ProveIt: You are verified.', iso(30)),
                                               ('In', 'https://github.com/example/Anti-Leecher-for-Nicotine-ProveIt', iso(29)))
        self.assertEqual(human_checks([verified_then_link], now=now), [])
        german = ('In', 'Human check for your requested album "X". Reply only with this word / Antworte nur mit diesem Wort: X.', iso(5))
        self.assertEqual(human_checks([self.conversation(german)], now=now)[0]['status'], 'open')


class TorrentTests(unittest.TestCase):
    def test_releases_rank_by_seeders_and_drop_images_and_dead_torrents(self):
        from app.sources import normalize_torrents
        releases = [
            {'title': 'Artist - Album - 2024, FLAC (tracks), lossless', 'downloadUrl': 'http://p/1', 'seeders': 3, 'indexer': 'RuTracker', 'guid': 'a'},
            {'title': 'Artist - Album - 2024, MP3, 320 kbps', 'downloadUrl': 'http://p/2', 'seeders': 9, 'indexer': 'RuTracker', 'guid': 'b'},
            {'title': 'Artist - Album - 2024, FLAC (image+.cue), lossless', 'downloadUrl': 'http://p/3', 'seeders': 50, 'guid': 'c'},
            {'title': 'Artist - Album - 2024, APE (tracks)', 'downloadUrl': 'http://p/4', 'seeders': 50, 'guid': 'd'},
            {'title': 'Artist - Album - 2024, FLAC (tracks)', 'downloadUrl': 'http://p/5', 'seeders': 0, 'guid': 'e'},
            {'title': '[TR24][OF] Artist - Album - 2024 (Jazz)', 'downloadUrl': 'http://p/6', 'seeders': 1, 'guid': 'f'},
        ]
        found = normalize_torrents(releases)
        self.assertEqual([(c['format'], c['bitrate'], c['seeders']) for c in found], [('mp3', 320, 9), ('flac', None, 3), ('flac', None, 1)])
        self.assertEqual(found[0]['username'], 'RuTracker')

    def test_a_discography_torrent_downloads_only_the_requested_album(self):
        from app.sources import torrent_selection
        files = [{'index': 0, 'name': 'Artist - Discography/2019 - First/01 A.flac'},
                 {'index': 1, 'name': 'Artist - Discography/2024 - Mid Spiral/CD1/01 B.flac'},
                 {'index': 2, 'name': 'Artist - Discography/2024 - Mid Spiral/CD2/01 C.flac'},
                 {'index': 3, 'name': 'Artist - Discography/2024 - Mid Spiral (Remixes)/01 D.flac'},
                 {'index': 4, 'name': 'Artist - Discography/2024 - Mid Spiral/cover.jpg'}]
        self.assertEqual([f['index'] for f in torrent_selection(files, 'Mid Spiral')], [1, 2])
        with self.assertRaises(SourceError):
            torrent_selection(files, None)
        with self.assertRaises(SourceError):
            torrent_selection(files, 'Something Else')

    def test_single_file_cue_images_are_refused(self):
        from app.sources import torrent_selection
        with self.assertRaises(SourceError):
            torrent_selection([{'index': 0, 'name': 'Album/Album.flac'}, {'index': 1, 'name': 'Album/Album.cue'}], 'Album')

    def test_torrent_download_selects_files_waits_and_copies(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / 'torrents'
            (root / 'Disco' / 'Album').mkdir(parents=True)
            (root / 'Disco' / 'Album' / '01 A.flac').write_bytes(b'a' * 10)
            (root / 'Disco' / 'Album' / '02 B.flac').write_bytes(b'b' * 10)
            sources = Sources({'torrent_download_root': str(root), 'torrent_poll_seconds': 0})
            progress_of = {0: 0.0, 1: 0.0}
            calls, torrents = [], []

            def qbit(method, path, **kwargs):
                calls.append((path, kwargs.get('data')))
                if path == '/torrents/info':
                    return torrents
                if path == '/torrents/files':
                    for index in progress_of:
                        progress_of[index] = min(1.0, progress_of[index] + 0.5)
                    return [{'index': 0, 'name': 'Disco/Album/01 A.flac', 'size': 10, 'priority': 1, 'progress': progress_of[0]},
                            {'index': 1, 'name': 'Disco/Album/02 B.flac', 'size': 10, 'priority': 1, 'progress': progress_of[1]},
                            {'index': 2, 'name': 'Disco/Other/01 C.flac', 'size': 10, 'priority': 1, 'progress': 0}]
                return ''

            def add(candidate, tag):
                torrents.append({'hash': 'h1', 'name': 'Disco', 'state': 'stoppedDL', 'save_path': str(root), 'num_seeds': 2})

            candidate = {'id': 'c1', 'torrent_key': 'k' * 32, 'source': 'torrent', 'download_url': 'http://p/1',
                         'requested_album': 'Album', 'title': 'Disco'}
            with patch.object(Sources, '_qbit', side_effect=qbit), patch.object(Sources, '_add_torrent', side_effect=add), \
                    patch('app.sources.time.sleep'):
                paths = sources.download(candidate, Path(tmp) / 'out', lambda value: None, lambda: False)
            self.assertEqual([p.name for p in paths], ['001-01 A.flac', '002-02 B.flac'])
            self.assertIn(('/torrents/filePrio', {'hash': 'h1', 'id': '2', 'priority': 0}), calls)
            self.assertEqual(candidate['torrent_files'], [0, 1])
            self.assertFalse(any(path == '/torrents/delete' for path, _ in calls))   # keeps seeding

    def test_a_torrent_without_progress_is_removed_and_fails(self):
        with TemporaryDirectory() as tmp:
            sources = Sources({'torrent_download_root': tmp, 'torrent_poll_seconds': 0, 'download_stall_seconds': 0})
            calls, priority = [], {0: 1}

            def qbit(method, path, **kwargs):
                calls.append(path)
                if path == '/torrents/info':
                    return [{'hash': 'h1', 'name': 'Album', 'state': 'stalledDL', 'save_path': tmp, 'num_seeds': 0}]
                if path == '/torrents/filePrio':
                    priority[0] = kwargs['data']['priority']
                if path == '/torrents/files':
                    return [{'index': 0, 'name': 'Album/01 A.flac', 'size': 10, 'priority': priority[0], 'progress': 0}]
                return ''

            candidate = {'id': 'c1', 'source': 'torrent', 'download_url': 'http://p/1', 'requested_album': 'Album'}
            with patch.object(Sources, '_qbit', side_effect=qbit), patch('app.sources.time.sleep'):
                with self.assertRaisesRegex(SourceError, 'no progress'):
                    sources.download(candidate, Path(tmp) / 'out', lambda value: None, lambda: False)
            self.assertIn('/torrents/delete', calls)

    def test_torrent_search_without_an_enabled_tracker_is_an_error_not_an_empty_result(self):
        sources = Sources({'prowlarr_url': 'http://prowlarr:9696', 'prowlarr_api_key': 'k'})
        with patch.object(Sources, '_prowlarr', return_value=[{'name': 'RuTracker', 'enable': False}]):
            with self.assertRaisesRegex(SourceError, 'No torrent tracker'):
                sources.search('Artist Album', 'torrent', 'album')
        releases = [{'title': 'Artist - Album - 2024, FLAC (tracks)', 'downloadUrl': 'http://p/1', 'seeders': 2}]
        with patch.object(Sources, '_prowlarr', side_effect=[[{'enable': True}], releases]):
            self.assertEqual(len(sources.search('Artist Album', 'torrent', 'album')), 1)


class YouTubeMusicAlbumTests(unittest.TestCase):
    def test_album_search_returns_official_youtube_music_releases(self):
        pages = {
            'search': {'entries': [{'url': 'https://music.youtube.com/browse/A'}, {'url': 'https://music.youtube.com/browse/B'},
                                   {'url': 'https://music.youtube.com/browse/C'}]},
            'https://music.youtube.com/browse/A': {'id': 'OLAK5uy_full', 'title': 'Album - Mid Spiral', 'playlist_count': 2,
                                                   'entries': [{'channel': 'Band - Topic', 'duration': 100}, {'channel': 'Band - Topic', 'duration': 50}]},
            'https://music.youtube.com/browse/B': {'id': 'PLfanmade', 'title': 'Mid Spiral (fan playlist)', 'entries': [{'channel': 'someone'}]},
            'https://music.youtube.com/browse/C': {'id': 'OLAK5uy_ep', 'title': 'EP - Mid Spiral: Order', 'playlist_count': 6,
                                                   'entries': [{'channel': 'Band', 'duration': 10}]},
        }

        class FakeYDL:
            def __init__(self, options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                return pages['search' if 'search?q=' in url else url]

        with patch('app.sources.yt_dlp.YoutubeDL', FakeYDL):
            found = Sources({}).search('Band Mid Spiral', 'youtube', 'album')
        self.assertEqual([(c['title'], c['release_type'], c['artist'], c['file_count']) for c in found],
                         [('Mid Spiral', 'Album', 'Band', 2), ('Mid Spiral: Order', 'EP', 'Band', 6)])
        self.assertEqual(found[0]['url'], 'https://www.youtube.com/playlist?list=OLAK5uy_full')
        self.assertEqual(found[0]['duration'], 150)
