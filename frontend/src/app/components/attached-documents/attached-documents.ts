import { Component, Input, Output, EventEmitter } from '@angular/core';
import { CommonModule } from '@angular/common';

export interface AttachedDocument {
  id?: number;
  sessionId?: string;
  filename?: string;
  sizeBytes?: number;
  contentType?: string;
  status?: string;
  uploadedAt?: string;
}

export type FileKind =
  | 'pdf'
  | 'word'
  | 'sheet'
  | 'slides'
  | 'image'
  | 'text'
  | 'json'
  | 'html'
  | 'csv'
  | 'other';

const EXTENSION_KIND: Record<string, FileKind> = {
  pdf: 'pdf',
  docx: 'word',
  doc: 'word',
  xlsx: 'sheet',
  xls: 'sheet',
  pptx: 'slides',
  ppt: 'slides',
  png: 'image',
  jpg: 'image',
  jpeg: 'image',
  webp: 'image',
  bmp: 'image',
  gif: 'image',
  tiff: 'image',
  tif: 'image',
  svg: 'image',
  txt: 'text',
  md: 'text',
  log: 'text',
  rst: 'text',
  csv: 'csv',
  tsv: 'csv',
  json: 'json',
  html: 'html',
  htm: 'html',
};

@Component({
  selector: 'app-attached-documents',
  imports: [CommonModule],
  templateUrl: './attached-documents.html',
  styleUrl: './attached-documents.css',
})
export class AttachedDocuments {
  @Input() documents: AttachedDocument[] = [];
  @Input() processing: string[] = [];
  @Input() collapsed = false;

  @Output() addDocuments = new EventEmitter<void>();
  @Output() removeDocument = new EventEmitter<number>();
  @Output() collapsedChange = new EventEmitter<boolean>();

  toggleCollapsed() {
    this.collapsed = !this.collapsed;
    this.collapsedChange.emit(this.collapsed);
  }

  onAdd() {
    this.addDocuments.emit();
  }

  onRemove(id?: number) {
    if (id != null) {
      this.removeDocument.emit(id);
    }
  }

  fileKind(filename?: string): FileKind {
    if (!filename) return 'other';
    const ext = filename.split('.').pop()?.toLowerCase() ?? '';
    return EXTENSION_KIND[ext] ?? 'other';
  }

  isImage(filename?: string): boolean {
    return this.fileKind(filename) === 'image';
  }

  formatSize(sizeBytes?: number): string {
    if (sizeBytes == null) return '';
    if (sizeBytes < 1024) return `${sizeBytes} B`;
    if (sizeBytes < 1024 * 1024) return `${(sizeBytes / 1024).toFixed(1)} KB`;
    return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  fileTypeLabel(filename?: string): string {
    if (!filename) return 'FILE';
    const ext = filename.split('.').pop()?.toUpperCase() ?? '';
    return ext || 'FILE';
  }

  statusLabel(status?: string): string {
    if (!status) return '';
    if (status === 'indexed') return '✓ Indexed';
    return status;
  }

  statusClass(status?: string): string {
    if (status === 'indexed') return 'status-indexed';
    if (status === 'failed') return 'status-failed';
    return '';
  }
}