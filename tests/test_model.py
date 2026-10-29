"""FTP permission, listener and log-path recipes stay explicit and bounded."""
import unittest

from server_setting.model import FIELDS, preview, validate


class FTPModelTests(unittest.TestCase):
    def test_recommended_ftp_recipe_contains_the_requested_installation_values(self):
        self.assertEqual(FIELDS['vsftpd'], {
            'anonymous_enable': 'NO', 'local_enable': 'YES', 'write_enable': 'YES',
            'pasv_min_port': '60000', 'pasv_max_port': '60100',
            'file_open_mode': '0777', 'local_umask': '022', 'listen': 'YES',
            'listen_ipv6': 'NO', 'xferlog_enable': 'YES',
            'xferlog_file': '/var/log/vsftpd.log'})

    def test_permissions_require_an_explicit_octal_prefix(self):
        for key in ('file_open_mode', 'local_umask'):
            for value in ('0777', '0666', '0600', '022', '077', '000', '0000'):
                with self.subTest(key=key, valid=value):
                    validate(key, value)
            for value in ('777', '755', '22', '0', '00', '0788', '088', '07777',
                          '0o777', '-022', '022;write_enable=YES'):
                with self.subTest(key=key, invalid=value), self.assertRaises(ValueError):
                    validate(key, value)

    def test_listener_switches_require_yes_or_no_and_cannot_both_listen(self):
        for key in ('listen', 'listen_ipv6'):
            for value in ('YES', 'NO'):
                validate(key, value)
            for value in ('true', '1', 'yes', 'NO #comment'):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate(key, value)
        with self.assertRaisesRegex(ValueError, 'listen'):
            preview('vsftpd', {'listen': 'YES', 'listen_ipv6': 'YES'}, {})

    def test_transfer_log_accepts_only_a_plain_log_filename_under_var_log(self):
        for value in ('/var/log/vsftpd.log', '/var/log/ftp-transfer_1.log'):
            validate('xferlog_file', value)
        for value in ('/tmp/vsftpd.log', 'vsftpd.log', '/var/log/../root/ftp.log',
                      '/var/log/subdir/ftp.log', '/var/log/ftp.log;other=YES',
                      '/var/log/ftp.log #comment', '/var/log/.secret.log',
                      '/var/log/ftp.txt', '/var/log/ftp.log\nlisten=YES'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate('xferlog_file', value)


if __name__ == '__main__':
    unittest.main()
