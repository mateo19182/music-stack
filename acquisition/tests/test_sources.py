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
        sources = Sources({'download_timeout_seconds': 0.02, 'slskd_poll_seconds': 0.001})
        queued = {'id': 'queued', 'filename': 'Music\\Song.mp3', 'size': 5,
                  'state': 'Queued, Remotely', 'placeInQueue': 7, 'bytesTransferred': 2}
        old = {**queued, 'id': 'old', 'state': 'Completed, Errored'}
        candidate = {'source': 'soulseek', 'username': 'peer', 'files': [{'filename': queued['filename'], 'size': 5}]}
        events = []
        with TemporaryDirectory() as destination, patch.object(sources, '_transfers', return_value=[queued, old]), \
             patch.object(sources, '_slskd') as api:
            with self.assertRaisesRegex(SourceError, 'remote queue.*without adding another transfer'):
                sources.download(candidate, destination, events.append, lambda: False)
            api.assert_not_called()
        self.assertTrue(any('position 7' in event['message'] for event in events))
        self.assertEqual(candidate['transfer_ids'], ['queued'])

    def test_transfer_failure_describes_peer_and_retained_partials(self):
        for state, detail, expected in [
            ('Completed, Errored', 'User peer appears to be offline', 'offline or unavailable'),
            ('TimedOut', '', 'did not respond in time'),
            ('Completed, Rejected', 'Too many failed transfers today', 'refused the download'),
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
