import { Component, OnInit, inject } from '@angular/core';
import { MatBadgeModule } from '@angular/material/badge';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatToolbarModule } from '@angular/material/toolbar';
import { Sidebar } from './components/sidebar/sidebar';
import { ChatInterface } from './components/chat-interface/chat-interface';
import { Api } from './services/api/api';

export interface Expert {
  name: string;
  role: string;
  source: string;
  sourceCount: number;
  reason: string;
  initials: string;
  sources?: string[];
}

@Component({
  selector: 'app-root',
  imports: [Sidebar, ChatInterface, MatBadgeModule, MatButtonModule, MatCardModule, MatChipsModule, MatIconModule, MatProgressSpinnerModule, MatTooltipModule, MatToolbarModule],
  templateUrl: './app.html',
  styleUrls: ['./app.css', './pnc-brand.css'],
})
export class App implements OnInit {
  private api = inject(Api);

  readonly alerts = [
    { title: 'Policy refresh required', detail: 'Information Security Policy expires in 12 days.', tone: 'warning' },
    { title: 'New source indexed', detail: 'Q3 Treasury Operating Plan is ready to ask.', tone: 'success' },
  ];
  readonly changes = [
    { title: 'Vendor risk framework', detail: 'Updated today · Governance', icon: 'edit_note' },
    { title: 'Client onboarding playbook', detail: 'Updated yesterday · Operations', icon: 'description' },
    { title: 'Liquidity reporting guide', detail: 'Updated Sep 10 · Finance', icon: 'account_balance' },
  ];
  readonly searches = ['Treasury management', 'Client onboarding', 'Risk appetite', 'Cybersecurity controls'];

  experts: Expert[] = [];
  expertsLoaded = false;

  ngOnInit(): void {
    this.loadExperts();
  }

  loadExperts(): void {
    this.api.getTopExperts().subscribe({
      next: (response: any) => {
        this.experts = Array.isArray(response?.experts) ? response.experts : [];
        this.expertsLoaded = true;
      },
      error: () => {
        this.experts = [];
        this.expertsLoaded = true;
      },
    });
  }
}
