# Backups and exports: R2, because egress is free and the restore drill downloads a full dump on a bad day.
# `PGM_BACKUP_TARGET` points here per environment (staging gets its own prefix, so a restore cannot cross planes).

resource "cloudflare_r2_bucket" "backups" {
  account_id = var.cloudflare_account_id
  name       = "polygm-backups"
  location   = "EEUR"
}


#: Nothing is written here by Terraform: the bucket is the destination, and the writer is the nightly job on the
#: API host (`services/api/backup.py` → `pg_dump | age -r $PGM_BACKUP_RECIPIENT | rclone copyto r2:...`). Encryption
#: is by age with a recipient key the hosts do not hold, so a replica of the bucket is not a replica of the data.
